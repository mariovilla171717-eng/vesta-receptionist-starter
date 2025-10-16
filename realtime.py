# realtime.py — connects Twilio Media Stream to OpenAI Realtime (fast, human voice)
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets, base64

router = APIRouter()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")

async def connect_openai():
    """Create a WebSocket connection to OpenAI Realtime model."""
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    ws = await websockets.connect(url, extra_headers=headers)
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    print("🟢 Twilio connected")

    openai_ws = await connect_openai()
    print("🧠 Connected to OpenAI Realtime")

    async def forward_twilio_to_openai():
        """Receive audio from Twilio → send to OpenAI"""
        try:
            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                if data.get("event") == "media":
                    payload = data["media"]["payload"]
                    audio_bytes = base64.b64decode(payload)
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio_bytes).decode()
                    }))
                elif data.get("event") == "stop":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def forward_openai_to_twilio():
        """Receive audio from OpenAI → send back to Twilio"""
        try:
            async for message in openai_ws:
                event = json.loads(message)
                if event.get("type") == "response.audio.delta":
                    audio_chunk = base64.b64decode(event["audio"])
                    await twilio_ws.send_bytes(audio_chunk)
        except Exception as e:
            print("OpenAI stream ended:", e)

    await asyncio.gather(forward_twilio_to_openai(), forward_openai_to_twilio())
    print("🔴 Session ended")
