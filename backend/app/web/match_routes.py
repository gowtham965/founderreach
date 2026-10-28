"""The buttons on a startup card (no chat in between): draft, edit, sent, replied, skip, follow-ups, proof of work."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import get_db
from app.llm import usage
from app.models import Match, User
from app.agents import proof_of_work as pow_svc
from app.services import billing, matching, outreach

router = APIRouter()
log = logging.getLogger(__name__)


class DraftEditIn(BaseModel):
    subject: str | None = None
    body: str | None = None
    to_email: EmailStr | None = None
    to_name: str | None = None


class PowLinkIn(BaseModel):
    link: str = Field(min_length=10, max_length=500)


def act(fn):
    """Run a card action; turn the services' errors into the right HTTP answer."""
    try:
        return fn()
    except billing.PaywallError as exc:
        raise HTTPException(402, str(exc)) from exc
    except usage.UserBudgetExceeded as exc:  # their AI cost cap for today
        raise HTTPException(429, str(exc)) from exc
    except outreach.OutreachError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # e.g. the AI is unavailable while drafting
        log.exception("action failed")
        raise HTTPException(503, "That didn't work right now. Please try again in a few minutes.") from exc


def card_of(o) -> dict:
    return matching.match_card(o.match)


@router.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms = db.scalars(select(Match).where(Match.user_id == user.id).order_by(Match.created_at.desc())).all()
    return {"stats": outreach.stats(db, user), "matches": [matching.match_card(m) for m in ms]}


@router.post("/api/matches/{match_id}/draft")
def api_draft(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return card_of(act(lambda: outreach.draft(db, user, match_id)))


@router.patch("/api/matches/{match_id}/draft")
def api_edit_draft(match_id: int, body: DraftEditIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.to_email:
        act(lambda: outreach.set_recipient(db, user, match_id, body.to_email, body.to_name or ""))
    return card_of(act(lambda: outreach.edit_draft(db, user, match_id, body.subject, body.body)))


@router.post("/api/matches/{match_id}/sent")
def api_mark_sent(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The user sent the draft from their own email."""
    return card_of(act(lambda: outreach.mark_sent(db, user, match_id)))


@router.post("/api/matches/{match_id}/replied")
def api_mark_replied(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return card_of(act(lambda: outreach.mark_replied(db, user, match_id)))


@router.post("/api/matches/{match_id}/followup-sent")
def api_followup_sent(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return card_of(act(lambda: outreach.mark_followup_sent(db, user, match_id)))


@router.post("/api/matches/{match_id}/dm")
def api_dm(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return card_of(act(lambda: outreach.make_dm(db, user, match_id)))


@router.post("/api/matches/{match_id}/skip")
def api_skip(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return matching.match_card(act(lambda: outreach.skip(db, user, match_id)))


@router.post("/api/matches/{match_id}/stop-followups")
def api_stop(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return card_of(act(lambda: outreach.cancel_followups(db, user, match_id)))


# proof of work: ideas → a starter → their link goes into the email
@router.post("/api/matches/{match_id}/pow")
def api_pow_ideas(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return matching.match_card(act(lambda: pow_svc.suggest(db, user, match_id)))


@router.post("/api/matches/{match_id}/pow/{index}")
def api_pow_start(match_id: int, index: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return matching.match_card(act(lambda: pow_svc.start(db, user, match_id, index)))


@router.post("/api/matches/{match_id}/pow-link")
def api_pow_link(match_id: int, body: PowLinkIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return matching.match_card(act(lambda: pow_svc.attach(db, user, match_id, body.link)))
