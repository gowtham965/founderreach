"""What happened to each email: sent, replied, follow-up sent, skipped, follow-ups stopped; and the totals."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Match, Outreach, User, aware, now
from app.services.outreach.common import OutreachError, _owned_match
from app.services.outreach.followups import _followup_days


def mark_sent(db: Session, user: User, match_id: int) -> Outreach:
    """The user sent the draft from their own email."""
    m = _owned_match(db, user, match_id)
    o = m.outreach
    if o is None or o.status != "draft" or not o.body:
        raise OutreachError("There's no draft for that startup yet")
    days = _followup_days()
    o.status, o.sent_at, o.error = "sent", now(), ""
    o.next_followup_at = o.sent_at + timedelta(days=days[0]) if days else None
    m.status = "sent"
    db.commit()
    return o


def mark_replied(db: Session, user: User, match_id: int, snippet: str = "") -> Outreach:
    m = _owned_match(db, user, match_id)
    o = m.outreach
    if o is None or o.status not in ("sent", "replied"):
        raise OutreachError("Mark the email as sent first")
    o.status, o.replied_at, o.reply_snippet = "replied", now(), snippet[:500]
    o.next_followup_at, o.followup_draft = None, None
    m.status = "replied"
    db.commit()
    return o


def mark_followup_sent(db: Session, user: User, match_id: int) -> Outreach:
    m = _owned_match(db, user, match_id)
    o = m.outreach
    if o is None or o.status != "sent" or not o.followup_draft:
        raise OutreachError("There's no follow-up waiting for that startup")
    days = _followup_days()
    o.followups_sent += 1
    o.followup_draft = None
    nxt = days[o.followups_sent] if o.followups_sent < len(days) else None
    o.next_followup_at = aware(o.sent_at) + timedelta(days=nxt) if nxt else None
    db.commit()
    return o


def skip(db: Session, user: User, match_id: int) -> Match:
    m = _owned_match(db, user, match_id)
    if m.outreach and m.outreach.status in ("approved", "sent", "replied"):
        raise OutreachError("Already sent; stop the follow-up reminders instead")
    m.status = "skipped"
    if m.outreach:
        m.outreach.status = "cancelled"
    db.commit()
    return m


def cancel_followups(db: Session, user: User, match_id: int) -> Outreach:
    m = _owned_match(db, user, match_id)
    if m.outreach is None:
        raise OutreachError("Nothing was sent to that startup")
    m.outreach.followups_enabled = False
    m.outreach.next_followup_at = None
    m.outreach.followup_draft = None
    db.commit()
    return m.outreach


def stats(db: Session, user: User) -> dict:
    rows = db.scalars(select(Outreach).where(Outreach.user_id == user.id)).all()
    return {
        "matches": db.scalar(select(func.count(Match.id)).where(Match.user_id == user.id)) or 0,
        "drafts": sum(o.status == "draft" for o in rows),
        "sent": sum(o.status in ("sent", "replied") for o in rows),
        "replies": sum(o.status == "replied" for o in rows),
        "followups_sent": sum(o.followups_sent for o in rows),
        "followups_due": sum(bool(o.followup_draft) for o in rows),
    }
