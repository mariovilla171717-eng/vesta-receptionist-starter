# realtime.py — Twilio <-> OpenAI Realtime with: barge-in + English-by-default (PCMU μ-law 8k)

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets

router = APIRouter()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

async def connect_openai():
    # Current GA realtime model & session schema
    url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    headers = { "Authorization": f"Bearer {OPENAI_KEY}" }
    ws = await websockets.connect(url, extra_headers=headers)

    # Session: μ-law 8kHz in/out, server VAD, ask for audio output, set English default
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
                "Default to ENGLISH. If the caller clearly speaks another language or asks, switch politely; "
                "otherwise stay in English. Be warm, concise, one question at a time, respond quickly. "
                "When interrupted, stop speaking immediately."
            ),
        }
    }))
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # Track speaking state and current response (for cancel)
    speaking = False
    current_response_id = None

    # --- wait for Twilio 'start' so we have streamSid ---
    while stream_sid is None:
        start_msg = await twilio_ws.receive_text()
        start_data = json.loads(start_msg)
        if start_data.get("event") == "start":
            stream_sid = start_data["start"]["streamSid"]

    openai_ws = await connect_openai()

    # Auto-commit after short pause (fast replies)
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
            # finalize the caller turn and request a reply
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await openai_ws.send(json.dumps({"type": "response.create"}))
        except asyncio.CancelledError:
            pass

    async def cancel_current_response():
        """Cancel bot speech immediately (barge-in)."""
        nonlocal speaking, current_response_id
        try:
            # Prefer targeted cancel if we have an id, else generic cancel
            if current_response_id:
                await openai_ws.send(json.dumps({"type": "response.cancel", "response": {"id": current_response_id}}))
            else:
                await openai_ws.send(json.dumps({"type": "response.cancel"}))
        except:
            pass
        speaking = False
        current_response_id = None

    async def twilio_to_openai():
        """Caller audio -> OpenAI. If caller talks while bot is speaking, cancel bot (barge-in)."""
        try:
            # Non-silent greeting so callers hear something right away
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
                    # If user starts speaking while bot is talking -> barge-in cancel
                    if speaking:
                        await cancel_current_response()

                    # Append μ-law frame and schedule quick commit (fast turn-taking)
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"]  # base64 μ-law 8k from Twilio
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
                await openai_ws.send(json.dumps({"type": "response.create"}))
            except:
                pass

    async def openai_to_twilio():
        """OpenAI audio -> Twilio. Track response ids & speaking state for barge-in."""
        nonlocal speaking, current_response_id
        try:
            async for message in openai_ws:
                event = json.loads(message)
                t = event.get("type")

                # Response lifecycle: capture id so we can cancel
                if t == "response.created":
                    # some schemas put id under event["response"]["id"]; fallback to event["id"]
                    current_response_id = (event.get("response") or {}).get("id") or event.get("id")

                # Streaming audio chunks (GA name)
                if t == "response.output_audio.delta":
                    payload_b64 = event.get("delta")
                    if payload_b64:
                        speaking = True
                        await twilio_ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload_b64}
                        }))

                elif t == "response.output_audio.done":
                    speaking = False
                    current_response_id = None
                    # Clear buffer so we can listen for the next user turn
                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

        except Exception:
            pass

    await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    # cleanup
    try: await openai_ws.close()
    except: pass
    try: await twilio_ws.close()
    except: pass
