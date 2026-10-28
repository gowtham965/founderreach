"""What the WhatsApp bot does with each incoming message.

Same agent and guardrails as the web chat. FounderReach never sends email: drafts come with
an "Open in Gmail" link and the user sends them from their own inbox. The dashboard is a
one-time sign-in link.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.agents import chat as agent
from app.auth import create_token
from app.config import get_settings
from app.models import ChatMessage, User, now
from app.services import discovery, profile as profile_svc, whatsapp

log = logging.getLogger(__name__)

DASHBOARD_RE = re.compile(r"^\s*(dashboard|pipeline|website|web|login|log in|open (the )?(app|website|dashboard))\s*[.!?]*\s*$", re.I)


def welcome(user: User) -> str:
    first = (user.name or "").split()[0] if (user.name or "").strip() else "there"
    return (f"ನಮಸ್ಕಾರ {first} 👋 I'm FounderReach. I find {discovery.focus_label()} that just raised and fit you, "
            "and write the email to their founder for you to send from your own Gmail.\n\n"
            "To start, send me your *resume* or *LinkedIn PDF* (tap 📎 → Document), paste your LinkedIn profile link, "
            "and tell me what you want next: kind of role, sectors, remote or in-office.\n\n"
            "I never send anything myself: you send each email from your own inbox.")


def dashboard_link(user: User) -> str:
    token = create_token(user.id, purpose="magic", minutes=15, epoch=user.session_epoch)
    return f"{get_settings().base_url.rstrip('/')}/api/auth/magic?token={token}"


def _reply_for_document(db: Session, user: User, msg: dict) -> str:
    doc = msg.get("document") or {}
    name, mime = doc.get("filename") or "file", doc.get("mime_type") or ""
    if mime not in ("application/pdf", "text/plain", "text/markdown") and not name.lower().endswith((".pdf", ".txt", ".md")):
        return "I can read PDF or text files. Send your resume or LinkedIn PDF as a document (📎 → Document)."
    data, mime = whatsapp.download_media(doc["id"])
    text = profile_svc.document_text(data, name, mime)
    if len(text) < 200:
        return "Couldn't read much text from that file. Is it a scanned image? Try a text PDF."
    db.add(ChatMessage(user_id=user.id, role="user", content=f"[attached: {name}]"))
    db.commit()
    p, kind = profile_svc.ingest_document(db, user, text)
    reply = profile_svc.upload_reply(p, kind)
    db.add(ChatMessage(user_id=user.id, role="assistant", content=reply))
    db.commit()
    return reply


def _reply_for_text(db: Session, user: User, text: str) -> str:
    if DASHBOARD_RE.match(text):
        return f"Here's your dashboard (link works for 15 minutes, just for you):\n{dashboard_link(user)}"
    result = agent.respond(db, user, text, channel="whatsapp")
    reply = result.get("reply", "").strip()
    cards = whatsapp.render_cards(result.get("cards") or [])
    return "\n\n".join(x for x in (reply, cards) if x)


def handle(db: Session, msg: dict, contact_name: str = "") -> None:
    """One incoming WhatsApp message → at most one reply (split if long)."""
    phone = whatsapp.normalize_phone(msg.get("from", ""))
    if not phone or whatsapp.already_handled(db, msg.get("id", "")):
        return
    user, created = whatsapp.find_or_create_user(db, phone, contact_name)
    user.whatsapp_last_inbound_at = now()
    db.commit()

    parts: list[str] = []
    if created:
        text = welcome(user)
        db.add(ChatMessage(user_id=user.id, role="assistant", content=text))
        db.commit()
        parts.append(text)
    else:  # what happened while they were away, before answering
        notices = whatsapp.pending_notices(db, user)
        if notices:
            parts.append("📬 *While you were away*\n" + "\n\n".join(notices))

    kind = msg.get("type")
    try:
        if kind == "text":
            body = (msg.get("text") or {}).get("body", "").strip()
            if body and not (created and len(body) < 40):  # a first "hi" just gets the welcome
                parts.append(_reply_for_text(db, user, body))
        elif kind == "document":
            parts.append(_reply_for_document(db, user, msg))
        elif not created:
            parts.append("I can read text messages and PDF documents for now. Send your resume as a document (📎 → Document).")
    except Exception:
        log.exception("WhatsApp message from user %s failed", user.id)
        db.rollback()
        parts.append("Sorry, something went wrong on my side. Try again in a minute.")

    reply = "\n\n".join(p for p in parts if p)
    if reply:
        try:
            whatsapp.send_text(user, reply)
        except whatsapp.WhatsAppError as exc:
            log.error("WhatsApp reply to user %s failed: %s", user.id, exc)
