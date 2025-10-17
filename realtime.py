# realtime.py — Twilio bidirectional stream <-> OpenAI Realtime with auto-turns (μ-law 8kHz)
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets

router = APIRouter()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

# ---- OpenAI session ----
async def connect_openai():
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    ws = await websockets.connect(url, extra_headers=headers)

    # Configure: 8kHz μ-law in/out, fast voice, server VAD
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 150, "silence_duration_ms": 350},
            "input_audio_format": {"type": "g711_ulaw", "sample_rate_hz": 8000},
            "output_audio_format": {"type": "g711_ulaw", "sample_rate_hz": 8000},
            "voice": "alloy",
            "instructions": (
                "You are a premium human receptionist for Vesta. "
                "Greet warmly, keep sentences short, ask one question at a time, and respond fast. "
                "If caller pauses, jump in quickly. Never sound like a voicemail."
            ),
        }
    }))
    return ws

# ---- WebSocket endpoint for Twilio <Connect><Stream> ----
@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # Read Twilio "start" first to get streamSid
    async def read_twilio_start():
        nonlocal stream_sid
        while stream_sid is None:
            msg = await twilio_ws.receive_text()
            data = json.loads(msg)
            if data.get("event") == "start":
                stream_sid = data["start"]["streamSid"]
                break
        return True

    await read_twilio_start()
    openai_ws = await connect_openai()

    # --- Auto-commit after brief silence (debounce) ---
    silence_delay_ms = 500  # ~0.5s after last audio frame
    commit_task = None
    commit_lock = asyncio.Lock()

    async def schedule_commit():
        nonlocal commit_task
        # cancel previous scheduled commit
        if commit_task and not commit_task.done():
            commit_task.cancel()
        commit_task = asyncio.create_task(commit_after_delay())

    async def commit_after_delay():
        try:
            await asyncio.sleep(silence_delay_ms / 1000.0)
            async with commit_lock:
                # finalize the caller's turn and ask model to respond
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await openai_ws.send(json.dumps({"type": "response.create"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # --- Pipelines ---
    async def twilio_to_openai():
        """Caller audio from Twilio -> OpenAI (μ-law pass-through)"""
        try:
            # Send an initial greeting right away so calls aren't silent
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {"instructions": "Hi, thanks for calling Vesta. How can I help you today?"}
            }))

            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                ev = data.get("event")
                if ev == "media":
                    # Append μ-law frame (Twilio's payload is base64 g711 μ-law 8k)
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"]
                    }))
                    # every frame resets the silence timer
                    await schedule_commit()
                elif ev == "stop":
                    break
                # ignore "mark" and others
        except WebSocketDisconnect:
            pass
        finally:
            # On disconnect, try to close out a turn
            try:
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await openai_ws.send(json.dumps({"type": "response.create"}))
            except:
                pass

    async def openai_to_twilio():
        """AI audio -> Twilio (μ-law base64 in Twilio media frames)"""
        try:
            async for message in openai_ws:
                event = json.loads(message)
                t = event.get("type")

                if t == "response.audio.delta":
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": event["audio"]}
                    }))

                elif t == "response.completed":
                    # Clear model input buffer so it listens for next user turn
                    try:
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    except:
                        pass

        except Exception:
            # end quietly
            pass

    await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    # Cleanup
    try:
        await openai_ws.close()
    except:
        pass
    try:
        await twilio_ws.close()
    except:
        pass
