"""Who may use the paid features right now: payments off, admin, trial (days, drafts, chats) or a pass."""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.auth import is_admin
from app.config import get_settings
from app.models import User, aware, now
from app.services.billing.plans import PLANS, PaywallError, price_line


def _provider() -> str:
    return "phonepe" if get_settings().payment_provider.lower() == "phonepe" else "razorpay"


def payments_on() -> bool:
    """Payments (and the trial limits) are on once the active gateway's keys are set."""
    s = get_settings()
    if _provider() == "phonepe":
        return bool(s.phonepe_client_id and s.phonepe_client_secret)
    return bool(s.razorpay_key_id and s.razorpay_key_secret)


def _pass_active(user: User, at: datetime) -> bool:
    return bool(user.pass_until and aware(user.pass_until) > at)


def _trial_end(user: User) -> datetime | None:
    if not user.trial_started_at:
        return None
    return aware(user.trial_started_at) + timedelta(days=get_settings().trial_days)


def _start_trial(user: User, at: datetime) -> None:
    """The trial clock starts at the first paid action, not at sign-up."""
    if not user.trial_started_at:
        user.trial_started_at = at


def _locked_message(user: User, why: str) -> str:
    return (f"{why} Get a pass to keep going: {price_line()}. "
            "Tap “Get a pass” at the top of the app. Everything you already have stays here.")


def has_access(user: User, at: datetime | None = None) -> bool:
    """New matches and follow-up drafts: trial time or a pass (trial drafts don't matter here)."""
    at = at or now()
    if not payments_on() or is_admin(user) or _pass_active(user, at):
        return True
    end = _trial_end(user)
    return end is None or at < end  # no trial yet: the next paid action starts it


def require_access(db: Session, user: User) -> None:
    at = now()
    if not has_access(user, at):
        raise PaywallError(_locked_message(user, "Your free trial has ended."))
    if payments_on():
        _start_trial(user, at)
        db.commit()


def use_draft(db: Session, user: User) -> None:
    """Called once per AI draft. On a trial, drafts are capped at TRIAL_DRAFTS."""
    at = now()
    if not payments_on() or is_admin(user) or _pass_active(user, at):
        return
    require_access(db, user)
    used = user.trial_drafts_used or 0
    if used >= get_settings().trial_drafts:
        raise PaywallError(_locked_message(user, f"You've used the {get_settings().trial_drafts} free drafts in your trial."))
    user.trial_drafts_used = used + 1
    db.commit()


def use_chat(db: Session, user: User) -> None:
    """Called once per chat message (web or WhatsApp). Chat is most of the AI bill, so trials get TRIAL_CHATS
    messages, and after the trial chat needs a pass. Buttons on the cards (sent, replied, skip) stay free."""
    at = now()
    if not payments_on() or is_admin(user) or _pass_active(user, at):
        return
    require_access(db, user)
    used = user.trial_chats_used or 0
    if used >= get_settings().trial_chats:
        raise PaywallError(_locked_message(user, f"You've used the {get_settings().trial_chats} free chat messages in your trial."))
    user.trial_chats_used = used + 1
    db.commit()


def summary(user: User) -> dict:
    """What the app shows in the plan pill and the passes sheet."""
    at = now()
    s = get_settings()
    end = _trial_end(user)
    return {
        "payments": payments_on(),
        "admin": is_admin(user),  # never limited or charged
        "gateway": "PhonePe" if _provider() == "phonepe" else "Razorpay",
        "pass_until": aware(user.pass_until).isoformat() if _pass_active(user, at) else None,
        "trial_days": s.trial_days,
        "trial_ends": end.isoformat() if end else None,
        "trial_drafts_left": max(s.trial_drafts - (user.trial_drafts_used or 0), 0),
        "trial_chats_left": max(s.trial_chats - (user.trial_chats_used or 0), 0),
        "access": has_access(user, at),
        "plans": [{"id": k, "price": p // 100, "days": d, "label": label} for k, (p, d, label) in PLANS.items()],
    }
