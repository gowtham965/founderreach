"""WhatsApp (Meta Cloud API) webhook: Meta's verification check, and incoming messages."""
import hmac
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.db import SessionLocal
from app.services import wa_bot, whatsapp

router = APIRouter()


@router.get("/api/whatsapp/webhook", include_in_schema=False)
def whatsapp_verify(request: Request):
    """Meta's one-time check when you save the webhook URL."""
    q = request.query_params
    token = get_settings().whatsapp_verify_token
    if q.get("hub.mode") == "subscribe" and token and hmac.compare_digest(q.get("hub.verify_token", ""), token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


def _handle(msg: dict, name: str) -> None:
    with SessionLocal() as db:
        wa_bot.handle(db, msg, name)


@router.post("/api/whatsapp/webhook", include_in_schema=False)
async def whatsapp_incoming(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not whatsapp.signature_ok(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Bad signature")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "Not JSON")
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:  # delivery "statuses" are ignored
                # answer after acknowledging: Meta retries webhooks that take too long
                background.add_task(_handle, msg, names.get(msg.get("from"), ""))
    return {"ok": True}
