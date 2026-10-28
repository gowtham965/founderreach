"""Shared pieces: the error type, owned-match lookup, confirmation and daily-draft checks, notify, compose links."""
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ChatMessage, Match, User


class OutreachError(Exception):
    pass


def with_linkedin(body: str, user: User) -> str:
    """Put the user's LinkedIn link under their sign-off: founders nearly always click it."""
    url = ((user.profile.links if user.profile else None) or {}).get("linkedin")
    if not url or url.split("://", 1)[-1].removeprefix("www.") in body:
        return body
    return f"{body}\n{url}"


def notify(db: Session, user_id: int, text: str) -> None:
    db.add(ChatMessage(user_id=user_id, role="assistant", content=text, unread=True))
    db.commit()


def _owned_match(db: Session, user: User, match_id: int) -> Match:
    m = db.get(Match, match_id)
    if m is None or m.user_id != user.id:
        raise OutreachError(f"No match #{match_id} for this user")
    return m


def require_confirmed(user: User) -> None:
    if not user.email_verified_at:
        raise OutreachError(f"Confirm your email first to see founder emails and drafts. We sent a link to {user.email}")


def _use_draft_quota(db: Session, user: User) -> None:
    """Count AI drafts per day (new drafts, rewrites and follow-ups); refuse past the cap."""
    today = datetime.now(timezone.utc).date().isoformat()
    used = user.drafts_used if user.drafts_day == today else 0
    if used >= get_settings().max_drafts_per_day:
        raise OutreachError("That's today's draft limit. More tomorrow morning")
    user.drafts_day, user.drafts_used = today, used + 1


def compose_links(to: str, subject: str, body: str) -> dict:
    """Prefilled 'new message' links: the user's own Gmail (web or app) and their default mail app."""
    gmail = "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body})
    mailto = f"mailto:{quote(to)}?" + urlencode({"subject": subject, "body": body}, quote_via=quote)
    return {"gmail": gmail, "mailto": mailto}
