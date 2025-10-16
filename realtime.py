# realtime.py — Twilio bidirectional stream <-> OpenAI Realtime using G.711 μ-law (8kHz)
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets

router = APIRouter()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

async def connect_openai():
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    ws = await websockets.connect(url, extra_headers=headers)
    # Configure session for 8kHz μ-law in/out + fast VAD + a nice voice
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 150, "silence_duration_ms": 350},
            "input_audio_format": {"type": "g711_ulaw", "sample_rate_hz": 8000},
            "output_audio_format": {"type": "g711_ulaw", "sample_rate_hz": 8000},
            "voice": "alloy",     # we can change this later to your favorite
            "instructions": (
                "You are a premium human receptionist. Be warm, brief, and fast. "
                "Confirm details clearly and ask one question at a time."
            ),
        }
    }))
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # Wait for Twilio's initial 'connected'/'start' messages to get streamSid
    async def read_twilio_start():
        nonlocal stream_sid
        while stream_sid is None:
            msg = await twilio_ws.receive_text()
            data = json.loads(msg)
            event = data.get("event")
            if event == "start":
                stream_sid = data["start"]["streamSid"]
                break
        return True

    await read_twilio_start()

    # Connect to OpenAI after we know the Twilio streamSid
    openai_ws = await connect_openai()

    async def twilio_to_openai():
        """Send caller audio from Twilio -> OpenAI (μ-law 8k)"""
        try:
            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                ev = data.get("event")
                if ev == "media":
                    # Twilio media payload is base64 g711 μ-law 8k
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"],  # pass-through (already μ-law)
                    }))
                elif ev == "mark":
                    continue
                elif ev == "stop":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            # Commit any buffered audio and ask the model to respond
            try:
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await openai_ws.send(json.dumps({"type": "response.create"}))
            except:
                pass

    async def openai_to_twilio():
        """Send AI audio back to Twilio -> caller (μ-law 8k)"""
        try:
            async for message in openai_ws:
                event = json.loads(message)
                t = event.get("type")

                # Stream audio deltas directly back to Twilio as 'media' frames
                if t == "response.audio.delta":
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": event["audio"]}
                    }))

                # When a turn is complete, tell OpenAI to start listening again
                elif t == "response.completed":
                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

                # You can log text transcripts if you want:
                # if t == "response.output_text.delta": print(event.get("delta",""), end="", flush=True)

        except Exception as e:
            # End gracefully
            try:
                await twilio_ws.send_text(json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": "openai_end"}}))
            except:
                pass

    await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    try:
        await openai_ws.close()
    except:
        pass
    try:
        await twilio_ws.close()
    except:
        pass
