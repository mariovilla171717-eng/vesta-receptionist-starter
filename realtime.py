# realtime.py — Twilio <-> OpenAI Realtime
# Clean, stable version for Vesta demo (no greeting here)
# Codec: PCMU (G.711 μ-law) 8kHz

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets, base64

router = APIRouter()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

async def connect_openai():
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    ws = await websockets.connect(url, extra_headers=headers)
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "voice": "alloy",
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "turn_detection": {"type": "server_vad"},
            "instructions": (
                "You are Vesta, a friendly, warm, human-sounding AI receptionist. "
                "You represent the company Vesta AI, which builds AI-powered receptionists "
                "for businesses such as barbershops, clinics, and salons. "
                "Your job is to sound completely human — natural, confident, and professional. "
                "When people call, explain that you are an AI receptionist that can handle calls, "
                "book appointments, answer client questions, and help businesses save time. "
                "If the caller shows interest, offer to schedule a short 15-minute Zoom demo call "
                "with our integration team. Confirm their preferred time and collect their name, "
                "phone number, and email. Be sure to confirm the time politely. "
                "After the call, the system will send them a Zoom link automatically. "
                "Never mention you are connected to OpenAI or any tech; stay brand consistent as Vesta."
            )
        }
    }))
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    while stream_sid is None:
        msg = await twilio_ws.receive_text()
        data = json.loads(msg)
        if data.get("event") == "start":
            stream_sid = data["start"]["streamSid"]

    openai_ws = await connect_openai()

    async def twilio_to_openai():
        try:
            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                if data.get("event") == "media":
                    payload = data["media"]["payload"]
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload
                    }))
                elif data.get("event") == "stop":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def openai_to_twilio():
        try:
            async for message in openai_ws:
                event = json.loads(message)
                if event.get("type") == "response.output_audio.delta":
                    delta = event.get("delta")
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": delta}
                    }))
        except Exception:
            pass

    await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    try: await openai_ws.close()
    except: pass
    try: await twilio_ws.close()
    except: pass
