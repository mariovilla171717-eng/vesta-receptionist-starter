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

from realtime import router as realtime_router
app.include_router(realtime_router)
