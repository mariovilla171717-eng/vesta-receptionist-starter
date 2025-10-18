# realtime.py — Twilio <-> OpenAI Realtime
# FINAL STABLE VERSION — reliable greeting + strict half-duplex barge-in
# Codec: PCMU (G.711 μ-law) 8kHz

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio, os, json, websockets, base64, time

router = APIRouter()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

# ---------- μ-law decoding for volume detection ----------
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

# ---------- Connect to OpenAI Realtime ----------
async def connect_openai():
    url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
    ws = await websockets.connect(url, extra_headers=headers)
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcmu"}},
                "output": {"format": {"type": "audio/pcmu"}, "voice": "alloy"}
            },
            "instructions": (
                "You are a professional, human-sounding receptionist for Vesta. "
                "Speak clearly and politely in English unless the caller prefers another language. "
                "Always respond quickly and stop talking immediately if the caller speaks."
            ),
        }
    }))
    return ws

# ---------- WebSocket Endpoint ----------
@router.websocket("/ws")
async def ws_endpoint(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None
    speaking = False
    suppress_outbound = False
    current_response_id = None
    loud_ms_accum = 0

    # Get streamSid from Twilio
    while stream_sid is None:
        msg = await twilio_ws.receive_text()
        data = json.loads(msg)
        if data.get("event") == "start":
            stream_sid = data["start"]["streamSid"]

    openai_ws = await connect_openai()

    # ---------- Reliable Greeting ----------
    async def send_greeting():
        greeting = "Hi, thank you for calling Vesta. How can I help you today?"
        # Schema A
        await openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {"modalities": ["audio"], "instructions": greeting}
        }))
        # Schema B
        await openai_ws.send(json.dumps({
            "type": "response.create",
            "modalities": ["audio"],
            "instructions": greeting
        }))

    await send_greeting()

    # ---------- Handle user silence / response timing ----------
    silence_delay_ms = 400
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
            await openai_ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))
            await openai_ws.send(json.dumps({"type": "response.create", "modalities": ["audio"]}))
        except asyncio.CancelledError:
            pass

    # ---------- Immediate Cancel ----------
    async def hard_cancel():
        nonlocal speaking, suppress_outbound, current_response_id
        suppress_outbound = True
        speaking = False
        try:
            if current_response_id:
                await openai_ws.send(json.dumps({"type": "response.cancel", "response": {"id": current_response_id}}))
            await openai_ws.send(json.dumps({"type": "response.cancel"}))
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
        except:
            pass
        current_response_id = None

    # ---------- Barge-in Detection ----------
    FRAME_MS = 20
    RMS_GATE = 5000       # lower = more sensitive to voice
    LOUD_MS_REQUIRED = 50 # 50ms of sustained voice triggers cancel

    async def twilio_to_openai():
        nonlocal loud_ms_accum, suppress_outbound, speaking
        try:
            while True:
                msg = await twilio_ws.receive_text()
                data = json.loads(msg)
                ev = data.get("event")

                if ev == "media":
                    mu_b64 = data["media"]["payload"]
                    mu_bytes = base64.b64decode(mu_b64)

                    # Detect caller voice
                    if speaking:
                        frame_rms = rms_from_mulaw_bytes(mu_bytes)
                        if frame_rms >= RMS_GATE:
                            loud_ms_accum += FRAME_MS
                            if loud_ms_accum >= LOUD_MS_REQUIRED:
                                await hard_cancel()
                                loud_ms_accum = 0
                        else:
                            loud_ms_accum = 0
                    else:
                        loud_ms_accum = 0

                    # Send caller audio to model
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
                await openai_ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))
                await openai_ws.send(json.dumps({"type": "response.create", "modalities": ["audio"]}))
            except:
                pass

    async def openai_to_twilio():
        nonlocal speaking, suppress_outbound, current_response_id
        try:
            async for message in openai_ws:
                event = json.loads(message)
                t = event.get("type")

                if t == "response.created":
                    suppress_outbound = False
                    current_response_id = (event.get("response") or {}).get("id") or event.get("id")

                elif t == "response.output_audio.delta":
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

    # ---------- Cleanup ----------
    try:
        await openai_ws.close()
    except:
        pass
    try:
        await twilio_ws.close()
    except:
        pass
