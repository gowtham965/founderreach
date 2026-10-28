"""The signed-in user: their account, chat history, chat messages and resume upload."""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import chat as agent
from app.auth import current_user, is_admin
from app.db import get_db
from app.llm import usage
from app.models import ChatMessage, Match, Payment, User
from app.services import billing, profile as profile_svc
from app.web.auth_routes import email_signup_open

router = APIRouter()
log = logging.getLogger(__name__)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@router.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = profile_svc.get_or_create_profile(db, user)
    return {"email": user.email, "name": user.name,
            "email_verified": bool(user.email_verified_at), "linkedin": (p.links or {}).get("linkedin"),
            "is_admin": is_admin(user), "can_confirm": email_signup_open(), "billing": billing.summary(user),
            "profile": {"summary": p.summary, "skills": p.skills, "highlights": p.highlights,
                        "target_roles": p.target_roles, "preferences": p.preferences, "ready": p.is_ready()}}


@router.delete("/api/me")
def delete_account(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """'Delete everything in one tap.'"""
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete()
    # payment records stay (refunds, accounts) but no longer point at the account
    db.query(Payment).filter(Payment.user_id == user.id).update({Payment.user_id: None})
    for m in db.scalars(select(Match).where(Match.user_id == user.id)).all():
        db.delete(m)
    db.delete(user)
    db.commit()
    return {"ok": True}


@router.get("/api/chat")
def chat_history(after: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The last 100 messages, or with ?after=<id> only newer ones (what the 30-second poll asks for)."""
    q = select(ChatMessage).where(ChatMessage.user_id == user.id)
    if after:
        rows = db.scalars(q.where(ChatMessage.id > after).order_by(ChatMessage.id).limit(100)).all()
    else:
        rows = list(reversed(db.scalars(q.order_by(ChatMessage.id.desc()).limit(100)).all()))
    return [{"id": r.id, "role": r.role, "content": r.content, "unread": r.unread, "at": r.created_at.isoformat(),
             "cards": agent.rebuild_cards(db, user, r.cards)} for r in rows]


@router.delete("/api/chat")
def clear_chat(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Clear the conversation. Matches, drafts and follow-ups live outside the chat, so they all stay (Pipeline)."""
    cleared = db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete()
    db.commit()
    return {"ok": True, "cleared": cleared}


@router.post("/api/chat")
def chat(body: ChatIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return agent.respond(db, user, body.message.strip())


@router.post("/api/resume")
async def upload_resume(file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > 5_000_000:
        raise HTTPException(413, "Resume must be under 5 MB")
    text = profile_svc.document_text(data, file.filename or "")
    if len(text) < 200:
        raise HTTPException(422, "Couldn't read much text from that file. Is it a scanned image? Try a text PDF.")
    db.add(ChatMessage(user_id=user.id, role="user", content=f"[attached: {file.filename}]"))
    db.commit()
    try:
        p, kind = profile_svc.ingest_document(db, user, text)
        msg = profile_svc.upload_reply(p, kind)
    except usage.UserBudgetExceeded as exc:
        db.rollback()
        msg = str(exc)
    except Exception:  # AI unavailable: keep the conversation whole and say so
        log.exception("resume reading failed for user %s", user.id)
        db.rollback()
        msg = "I couldn't read it right now because my AI side is unavailable. Please attach it again in a few minutes."
    db.add(ChatMessage(user_id=user.id, role="assistant", content=msg))
    db.commit()
    return {"reply": msg, "profile": profile_svc.profile_brief(user.profile)}
