# realtime.py — Twilio <-> OpenAI Realtime (final version)
# Greeting, full-duplex interrupt, stable voice.
# Codec: PCMU (G.711 μ-law) 8kHz.

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
                "You are a professional, warm, human-like receptionist for Vesta. "
                "Always greet the caller immediately by saying: "
                "'Hi, thanks for calling Vesta. How can I help you today?'. "
                "Speak in a natural, friendly tone, pause slightly after questions, "
                "and if the caller starts speaking, immediately stop talking and listen."
            )
        }
    }))
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # wait for start event from Twilio
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
