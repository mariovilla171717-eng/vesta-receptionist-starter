# realtime.py — Twilio bidirectional stream <-> OpenAI Realtime (μ-law 8k), audio guaranteed

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

    # Configure session: μ-law 8kHz in/out + server VAD + a natural voice
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 150, "silence_duration_ms": 350},
            "input_audio_format": {"type": "g711_ulaw", "sample_rate_hz": 8000},
            "output_audio_format": {"type": "g711_ulaw", "sample_rate_hz": 8000},
            "voice": "alloy",
            "instructions": (
                "You are a premium human receptionist for Vesta. "
                "Sound warm and human, keep answers short, ask one question at a time. "
                "React quickly; do not pause like voicemail."
            ),
        }
    }))
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # 1) Wait for Twilio "start" to get streamSid
    async def read_twilio_start():
        nonlocal stream_sid
        while stream_sid is None:
            msg = await twilio_ws.receive_text()
            data = json.loads(msg)
            if data.get("event") == "start":
                stream_sid = data["start"]["streamSid"]
                break
    await read_twilio_start()

    # 2) Connect to OpenAI
    openai_ws = await connect_openai()

    # Auto-commit after brief silence, so the bot responds quickly
    silence_delay_ms = 450
    commit_task = None
    commit_lock = asyncio.Lock()

    async def schedule_commit():
        nonlocal commit_task
        if commit_task and not commit_task.done():
            commit_task.cancel()
        commit_task = asyncio.create_task(commit_after_delay())

    async def commit_after_delay():
        try:
            await asyncio.sleep(silence_delay_ms / 1000)
            async with commit_lock:
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                # Ask specifically for AUDIO output to guarantee speech
                await openai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"modalities": ["audio"]}
                }))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def twilio_to_openai():
        """Caller audio -> OpenAI (μ-law pass-through) + immediate greeting"""
        try:
            # Say hello immediately so the line isn't silent
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {"modalities": ["audio"], "instructions": "Hi, thanks for calling Vesta. How can I help you today?"}
            }))

            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                ev = data.get("event")
                if ev == "media":
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"]  # μ-law base64 from Twilio
                    }))
                    await schedule_commit()
                elif ev == "stop":
                    break
                # ignore marks/others
        except WebSocketDisconnect:
            pass
        finally:
            try:
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await openai_ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))
            except:
                pass

    async def openai_to_twilio():
        """OpenAI audio -> Twilio (μ-law base64 in 'media' frames)."""
        try:
            async for message in openai_ws:
                event = json.loads(message)
                t = event.get("type")

                # Support both legacy and new event names
                if t in ("response.output_audio.delta", "response.audio.delta"):
                    audio_b64 = event.get("audio") or event.get("delta")
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": audio_b64}
                    }))

                elif t in ("response.output_audio.done", "response.audio.done"):
                    # Turn finished: clear input buffer so we can listen again
                    try:
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    except:
                        pass

                # (Optional) If you want to log text: handle "response.text.delta" here.
        except Exception:
            pass

    await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    # Cleanup
    try: await openai_ws.close()
    except: pass
    try: await twilio_ws.close()
    except: pass
