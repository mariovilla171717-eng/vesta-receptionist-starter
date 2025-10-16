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
    # Twilio hits this when a call arrives.
    base = os.environ.get("BASE_URL", "https://example.com")
    vr = VoiceResponse()
    vr.say("Connecting you now.")
    vr.connect().stream(url=f"wss://{base.split('://')[1]}/ws")
    return PlainTextResponse(str(vr), media_type="application/xml")

# --- include realtime route ---
from realtime import router as realtime_router
app.include_router(realtime_router)
