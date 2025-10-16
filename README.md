
# Vesta Receptionist - Starter (Voice Bridge)

This is the minimal starter needed to deploy a public URL that Twilio can call.
It returns TwiML with a `<Start><Stream>` to a WebSocket (`/ws`) that we'll wire to a realtime AI model in the next steps.

## Quick deploy (Render)
1) Create a new **Web Service** on Render.
2) Connect a GitHub repo with this code.
3) Environment:
   - `PYTHON_VERSION=3.11`
   - `BASE_URL=https://<your-render-service>.onrender.com`
4) Start command (auto from Procfile): `web: python main.py`
5) Health check path: `/health`

## Twilio
- In your phone number > Voice > "A call comes in": Webhook to `POST https://<render-domain>/voice`
