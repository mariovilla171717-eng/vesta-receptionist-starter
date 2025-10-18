import os
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/voice")
async def voice(request: Request):
    base = os.environ.get("BASE_URL", "https://example.com")
    host = base.split("://")[1]

    vr = VoiceResponse()
    # Twilio speaks this immediately (no waiting on the AI)
    vr.say("Hi, thanks for calling Vesta. How can I help you today?")
    # Start the bidirectional stream right after the greeting
    vr.connect().stream(url=f"wss://{host}/ws")
    return PlainTextResponse(str(vr), media_type="application/xml")

from realtime import router as realtime_router
app.include_router(realtime_router)
