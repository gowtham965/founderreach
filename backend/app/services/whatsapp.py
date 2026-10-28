"""WhatsApp channel (Meta WhatsApp Cloud API). WhatsApp-first: the phone number is the account.

The bot never starts a conversation. It only sends in reply to a message the person sent
within the last 24 hours (Meta's customer-service window), so no templates are needed.
Anything that happens in between (morning matches, founder replies) waits as an unread
chat notice and is delivered with the person's next message.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ChatMessage, User, WhatsAppInbound, aware, now

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
MAX_TEXT = 4000  # WhatsApp allows 4096 characters per text message


class WhatsAppError(Exception):
    pass


def is_configured() -> bool:
    s = get_settings()
    return bool(s.whatsapp_token and s.whatsapp_phone_number_id)


def chat_link(text: str = "Hi FounderReach") -> str | None:
    number = re.sub(r"\D", "", get_settings().whatsapp_number)
    return f"https://wa.me/{number}?text={quote(text)}" if number else None


def signature_ok(body: bytes, header: str | None) -> bool:
    """Meta signs every webhook with the app secret (X-Hub-Signature-256: sha256=<hex>)."""
    secret = get_settings().whatsapp_app_secret
    if not secret:  # without it anyone could post fake messages "from" any number
        log.error("WHATSAPP_APP_SECRET is not set: refusing WhatsApp webhooks until it is")
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(expected, header)


def normalize_phone(wa_id: str) -> str:
    return re.sub(r"\D", "", wa_id or "")


def placeholder_email(phone: str) -> str:
    # .invalid is reserved and never routes; WhatsApp users are identified by their number instead
    return f"wa-{phone}@whatsapp.invalid"


def find_or_create_user(db: Session, phone: str, name: str = "") -> tuple[User, bool]:
    user = db.scalar(select(User).where(User.phone == phone))
    if user:
        if name and not user.name:
            user.name = name
        return user, False
    user = User(email=placeholder_email(phone), name=name.strip(), password_hash="", phone=phone,
                email_verified_at=now())  # WhatsApp has proved they own the number
    db.add(user)
    db.commit()
    return user, True


def already_handled(db: Session, message_id: str) -> bool:
    """Record the id; True if we've seen it before (Meta retried the webhook)."""
    if db.scalar(select(WhatsAppInbound).where(WhatsAppInbound.wa_message_id == message_id)):
        return True
    db.add(WhatsAppInbound(wa_message_id=message_id))
    db.commit()
    return False


def window_open(user: User) -> bool:
    last = aware(user.whatsapp_last_inbound_at)
    return bool(last and datetime.now(timezone.utc) - last < WINDOW)


def _api(path: str) -> str:
    s = get_settings()
    return f"https://graph.facebook.com/{s.whatsapp_api_version}/{path}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().whatsapp_token}"}


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    parts, text = [], text.strip()
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        cut = cut if cut > limit // 2 else text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + ([text] if text else [])


def send_text(user: User, text: str) -> None:
    """Reply to the user. Refuses outside the 24-hour window: we never message first."""
    if not window_open(user):
        raise WhatsAppError("Outside the 24-hour reply window; FounderReach never messages first")
    if not is_configured():
        log.warning("WhatsApp not configured. Would send to %s:\n%s", user.phone, text)
        return
    for part in split_text(text):
        r = httpx.post(_api(f"{get_settings().whatsapp_phone_number_id}/messages"), headers=_headers(), timeout=20,
                       json={"messaging_product": "whatsapp", "to": user.phone, "type": "text",
                             "text": {"body": part, "preview_url": True}})
        if r.status_code >= 400:
            raise WhatsAppError(f"WhatsApp send failed ({r.status_code}): {r.text[:300]}")


def download_media(media_id: str) -> tuple[bytes, str]:
    """Two steps: ask Graph API for a short-lived URL, then fetch it with the same token."""
    meta = httpx.get(_api(media_id), headers=_headers(), timeout=20)
    meta.raise_for_status()
    info = meta.json()
    if int(info.get("file_size") or 0) > 5_000_000:
        raise WhatsAppError("That file is over 5 MB")
    data = httpx.get(info["url"], headers=_headers(), timeout=60, follow_redirects=True)
    data.raise_for_status()
    return data.content, info.get("mime_type", "")


def pending_notices(db: Session, user: User) -> list[str]:
    """Things that happened while they were away (unread chat notices), oldest first; marks them read."""
    rows = db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id, ChatMessage.unread.is_(True))
                      .order_by(ChatMessage.id)).all()
    for r in rows:
        r.unread = False
    db.commit()
    latest = [r.content for r in rows[-5:]]  # the newest few; the rest are in the dashboard
    if len(rows) > 5:
        latest.insert(0, f"(+{len(rows) - 5} older updates: type *dashboard* to see everything)")
    return latest


# ---------- turning agent cards into chat text ----------
CONF = {"verified": "verified ✓", "public": "published", "likely": "likely", "inbox": "shared inbox", "guess": "guess, may bounce",
        "user": "you added"}


def _match_line(c: dict) -> str:
    area = (c.get("location") or "").split(",")[0].strip()
    bits = " · ".join(b for b in (area, c.get("stage"), c.get("one_liner")) if b)
    via = f"\nvia Bangalore Startup Map: {c['source_url']}" if c.get("source") == "startup_map" and c.get("source_url") else ""
    return f"*#{c['match_id']} {c['name']}* ({round(c.get('score') or 0)} fit)\n{bits}\n_{c.get('reason', '')}_{via}"


def _draft_text(c: dict) -> str:
    o = c.get("outreach") or {}
    if o.get("status") == "sent" and o.get("followup_draft") and o.get("followup_compose"):
        return (f"⏰ *Follow-up for {c['name']}* (#{c['match_id']})\n\n{o['followup_draft']}\n\n"
                f"Best sent as a reply to your first email. Or open it ready to send:\n{o['followup_compose']['gmail']}\n\n"
                "Reply *follow-up sent* when it's out, or *they replied* to stop reminders.")
    if o.get("status") == "sent":
        return (f"✅ Marked as sent to {o.get('to_name') or c['name']}. "
                "I'll remind you to follow up in a few days, with a draft. Tell me if they reply.")
    if o.get("status") == "replied":
        return f"🎉 {c['name']} replied. Reminders stopped. Want a quick brief on them before you answer?"
    if o.get("status") != "draft" or not o.get("body"):
        return _match_line(c)
    to = f"{o.get('to_name') + ' ' if o.get('to_name') else ''}<{o.get('to_email') or 'no founder email yet'}>"
    conf = CONF.get(o.get("to_confidence") or "", "")
    open_link = f"\n\n📨 Open it in Gmail, ready to send:\n{o['compose']['gmail']}" if o.get("compose") else ""
    return (f"✉️ *Draft to {c['name']}* (#{c['match_id']})\nTo: {to}{' · ' + conf if conf else ''}\n"
            f"Subject: {o.get('subject', '')}\n\n{o.get('body', '')}{open_link}\n\n"
            "Send it from your own email, then reply *sent*. Or tell me what to change.")


def render_cards(cards: list[dict]) -> str:
    out = []
    for card in cards or []:
        if card.get("type") == "matches":
            out.append("\n\n".join(_match_line(c) for c in card.get("items", [])) +
                       "\n\nReply *draft* and a number (like *draft #12*) and I'll write the email.")
        elif card.get("item"):
            out.append(_draft_text(card["item"]))
    return "\n\n".join(out)
