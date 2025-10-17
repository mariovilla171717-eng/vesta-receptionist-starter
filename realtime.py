# realtime.py — Twilio <-> OpenAI Realtime
# Strict half-duplex barge-in (no overlap), noise-immune, guaranteed greeting.
# Codec: PCMU (G.711 μ-law) 8kHz.

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets, base64

router = APIRouter()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

# -------- μ-law decode (for barge-in gate) --------
SIGN_BIT = 0x80
QUANT_MASK = 0x0F
BIAS = 0x84

def mulaw_byte_to_pcm16(b: int) -> int:
    b = (~b) & 0xFF
    sign = b & SIGN_BIT
    exponent = (b >> 4) & 0x07
    mantissa = b & QUANT_MASK
    magnitude = ((mantissa << 4) + BIAS) << exponent
    sample = magnitude - BIAS
    return -sample if sign else sample

def rms_from_mulaw_bytes(mu_bytes: bytes) -> float:
    if not mu_bytes:
        return 0.0
    acc = 0
    n = len(mu_bytes)
    for bt in mu_bytes:
        s = mulaw_byte_to_pcm16(bt)
        acc += s * s
    return (acc / n) ** 0.5

# -------- OpenAI Realtime session --------
async def connect_openai():
    url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    headers = { "Authorization": f"Bearer {OPENAI_KEY}" }
    ws = await websockets.connect(url, extra_headers=headers)
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad", "silence_duration_ms": 500}
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": "alloy"
                }
            },
            "instructions": (
                "You are a premium human receptionist for Vesta. "
                "Default to ENGLISH unless the caller clearly prefers another language. "
                "Be warm, concise, one question at a time, respond quickly. "
                "If the caller interrupts, stop speaking immediately and listen."
            ),
        }
    }))
    return ws

@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # speaking state and suppression flag for strict half-duplex
    speaking = False
    suppress_outbound = False
    current_response_id = None

    # --- Twilio start: get streamSid ---
    while stream_sid is None:
        start_msg = await twilio_ws.receive_text()
        start_data = json.loads(start_msg)
        if start_data.get("event") == "start":
            stream_sid = start_data["start"]["streamSid"]

    openai_ws = await connect_openai()

    # --- guaranteed greeting (with fallback) ---
    async def send_greeting():
        await openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "modalities": ["audio"],
                "instructions": "Hi, thanks for calling Vesta. How can I help you today?"
            }
        }))
    await send_greeting()
    asyncio.create_task(asyncio.sleep(1.5))  # allow model to start output soon

    # --- quick reply after your pause ---
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

    async def hard_cancel():
        """Stop bot speech immediately (no more audio to Twilio) and cancel model turn."""
        nonlocal speaking, suppress_outbound, current_response_id
        suppress_outbound = True   # drop any further audio deltas from current response
        speaking = False
        try:
            if current_response_id:
                await openai_ws.send(json.dumps({"type": "response.cancel", "response": {"id": current_response_id}}))
            else:
                await openai_ws.send(json.dumps({"type": "response.cancel"}))
            # Clear any partial output/text buffers to avoid trailing speech
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
        except:
            pass
        current_response_id = None

    # --- barge-in gate (noise immune) ---
    FRAME_MS = 20
    LOUD_MS_REQUIRED = 120   # require ~120ms of real voice to trigger barge-in
    RMS_GATE = 7000          # raise if too sensitive; lower if not sensitive enough
    loud_ms_accum = 0

    async def twilio_to_openai():
        nonlocal loud_ms_accum, suppress_outbound
        try:
            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                ev = data.get("event")

                if ev == "media":
                    mu_b64 = data["media"]["payload"]
                    mu_bytes = base64.b64decode(mu_b64)

                    # If caller talks while bot is speaking, check gate then cancel
                    if speaking:
                        frame_rms = rms_from_mulaw_bytes(mu_bytes)
                        if frame_rms >= RMS_GATE:
                            loud_ms_accum += FRAME_MS
                            if loud_ms_accum >= LOUD_MS_REQUIRED:
                                await hard_cancel()           # stop sending any more audio to Twilio
                                loud_ms_accum = 0
                        else:
                            loud_ms_accum = 0
                    else:
                        loud_ms_accum = 0

                    # Append audio and prepare quick response after pause
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": mu_b64
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
        nonlocal speaking, suppress_outbound, current_response_id
        try:
            async for message in openai_ws:
                event = json.loads(message)
                t = event.get("type")

                if t == "response.created":
                    # New response: allow outbound again
                    suppress_outbound = False
                    current_response_id = (event.get("response") or {}).get("id") or event.get("id")

                if t == "response.output_audio.delta":
                    # Only forward audio if not suppressed (strict half-duplex)
                    if not suppress_outbound:
                        delta_b64 = event.get("delta")
                        if delta_b64:
                            speaking = True
                            await twilio_ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": delta_b64}
                            }))

                elif t == "response.output_audio.done":
                    speaking = False
                    current_response_id = None
                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

        except Exception:
            pass

    await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    # cleanup
    try: await openai_ws.close()
    except: pass
    try: await twilio_ws.close()
    except: pass
