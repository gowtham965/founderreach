"""The trial follows the person, not the account: deleting an account and signing up again (or using a Gmail alias)
continues the same trial. Stored as an HMAC fingerprint of the normalised email, so the address itself isn't kept."""
import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TrialUse, User, aware

GMAIL = ("gmail.com", "googlemail.com")


def normalise(email: str) -> str:
    """One mailbox, one identity: ravi.kumar+jobs@googlemail.com -> ravikumar@gmail.com; any +tag dropped."""
    local, _, domain = (email or "").strip().lower().partition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def identity(user: User) -> str:
    """WhatsApp accounts are their number; everyone else their normalised email."""
    key = f"phone:{user.phone}" if user.phone else normalise(user.email)
    return hmac.new(get_settings().secret_key.encode(), key.encode(), hashlib.sha256).hexdigest()


def remember(db: Session, user: User) -> None:
    """Copy the user's trial progress into the ledger (only ever forward: it never goes back to zero)."""
    if not user.trial_started_at and not user.trial_drafts_used and not user.trial_chats_used:
        return
    key = identity(user)
    row = db.scalar(select(TrialUse).where(TrialUse.identity == key))
    if row is None:
        row = TrialUse(identity=key, drafts_used=0, chats_used=0)
        db.add(row)
    started = aware(user.trial_started_at)
    if started and (not row.trial_started_at or started < aware(row.trial_started_at)):
        row.trial_started_at = started
    row.drafts_used = max(row.drafts_used or 0, user.trial_drafts_used or 0)
    row.chats_used = max(row.chats_used or 0, user.trial_chats_used or 0)
    db.commit()


def restore(db: Session, user: User) -> None:
    """A new account picks up where this person's earlier trial left off."""
    row = db.scalar(select(TrialUse).where(TrialUse.identity == identity(user)))
    if row is None:
        return
    user.trial_started_at = row.trial_started_at
    user.trial_drafts_used = row.drafts_used
    user.trial_chats_used = row.chats_used
    db.commit()
