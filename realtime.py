# realtime.py — minimal Twilio Media Streams receiver (no AI yet)
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json
import base64
import logging

router = APIRouter()

@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    logging.info("WS: connected")

    try:
        # Twilio will send JSON messages: "start" (once), many "media", then "stop".
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)

            event = data.get("event")
            if event == "start":
                # Twilio started streaming audio to us.
                logging.info(f"WS: start — streamSid={data.get('start', {}).get('streamSid')}")
            elif event == "media":
                # Audio frame from Twilio (base64-encoded PCM mulaw)
                _payload = data["media"]["payload"]  # we won't use it yet
            elif event == "stop":
                logging.info("WS: stop")
                break
            else:
                logging.info(f"WS: other event {event}")

    except WebSocketDisconnect:
        logging.info("WS: disconnected")
    finally:
        await ws.close()
