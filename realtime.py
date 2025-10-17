# realtime.py — Twilio <-> OpenAI Realtime (PCMU μ-law 8k), using official session schema

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets

router = APIRouter()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

async def connect_openai():
    # Use the GA model & session schema Twilio shows in their Python guide
    url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
    }
    ws = await websockets.connect(url, extra_headers=headers)

    # Session: server VAD; input/output = audio/pcmu (G.711 μ-law 8k); audio modality; voice
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"}
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": "alloy"
                }
            },
            "instructions": (
                "You are a premium human receptionist for Vesta. "
                "Sound warm and human, keep answers short, ask one question at a time, and react quickly."
            ),
        }
    }))
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # Wait for Twilio 'start' to get the streamSid
    while stream_sid is None:
        start_msg = await twilio_ws.receive_text()
        start_data = json.loads(start_msg)
        if start_data.get("event") == "start":
            stream_sid = start_data["start"]["streamSid"]

    openai_ws = await connect_openai()

    # Auto-commit after brief silence to trigger replies fast
    silence_delay_ms = 450
    commit_task = None

    async def schedule_commit():
        nonlocal commit_task
        if commit_task and not commit_task.done():
            commit_task.cancel()
        commit_task = asyncio.create_task(commit_after_delay())

    async def commit_after_delay():
        try:
            await asyncio.sleep(silence_delay_ms / 1000)
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await openai_ws.send(json.dumps({"type": "response.create"}))
        except asyncio.CancelledError:
            pass

    async def twilio_to_openai():
        """Caller audio -> OpenAI (pass-through μ-law base64 from Twilio). Also send a greeting immediately."""
        try:
            # Non-silent greeting so caller hears the bot right away
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio"],
                    "instructions": "Hi, thanks for calling Vesta. How can I help you today?"
                }
            }))

            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                ev = data.get("event")
                if ev == "media":
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"]  # base64 μ-law from Twilio
                    }))
                    await schedule_commit()
                elif ev == "stop":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            try:
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await openai_ws.send(json.dumps({"type": "response.create"}))
            except:
                pass

    async def openai_to_twilio():
        """OpenAI audio -> Twilio as 'media' frames (μ-law base64)."""
        try:
            async for message in openai_ws:
                event = json.loads(message)
                t = event.get("type")

                # GA event name for audio chunks:
                if t == "response.output_audio.delta":
                    # Twilio expects: {"event":"media","streamSid":...,"media":{"payload":<base64 μ-law>}}
                    payload_b64 = event.get("delta")
                    if payload_b64:
                        await twilio_ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload_b64}
                        }))

                elif t == "response.output_audio.done":
                    # turn finished; clear so we can listen for next user turn
                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

        except Exception:
            pass

    await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    # cleanup
    try: await openai_ws.close()
    except: pass
    try: await twilio_ws.close()
    except: pass
