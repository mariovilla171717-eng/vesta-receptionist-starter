
import os
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from twilio.twiml.voice_response import VoiceResponse, Start

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/voice")
async def voice(request: Request):
    # Twilio hits this when a call arrives.
    # It returns TwiML that starts a realtime media stream to our /ws endpoint.
    base = os.environ.get("BASE_URL", "https://example.com")
    vr = VoiceResponse()
    start = Start()
    # IMPORTANT: Twilio requires wss. Render gives you https -> use same host for wss.
    host = base.replace("https://", "").replace("http://", "")
    start.stream(url=f"wss://{host}/ws")
    vr.append(start)
    vr.say("Hi, connecting you now.")
    return PlainTextResponse(str(vr), media_type="application/xml")

# NOTE: The /ws endpoint is implemented in realtime.py (to be filled later with realtime AI wiring).
