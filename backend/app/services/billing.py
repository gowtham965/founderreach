"""Free trial, passes, and payments through Razorpay (Payment Links) or PhonePe (Standard Checkout v2).

Who can use the paid features (chat, new matches, AI drafts, follow-up drafts, LinkedIn notes):
  * everyone, while payments are off (no PhonePe keys set)
  * admins, always
  * anyone on a trial: TRIAL_DAYS from their first paid action, up to TRIAL_DRAFTS drafts and TRIAL_CHATS messages
  * anyone with a pass (pass_until in the future)
Everything already made stays visible either way.

Paying: create an order with the gateway (PAYMENT_PROVIDER), send the user to its page, and on
their return (or the gateway's webhook) ask the gateway for the order status from the server.
Only a fully paid order for the exact amount turns a pass on, and each order can do that once.
"""
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth import is_admin
from app.config import get_settings
from app.models import Payment, User, aware, now
from app.services.outreach import OutreachError

log = logging.getLogger(__name__)

# plan -> (price in paise, days, label)
PLANS = {
    "week": (29900, 7, "7-day pass"),
    "month": (69900, 30, "30-day pass"),
}


def price_line() -> str:
    """"₹299 for 7 days or ₹699 for 30 days": used in messages and the policy pages, so prices live only above."""
    return " or ".join(f"₹{p // 100} for {d} days" for p, d, _ in PLANS.values())
ORDER_ID_RE = re.compile(r"^FR\d+-[0-9a-f]{12}$")

_HOSTS = {
    "sandbox": {"token": "https://api-preprod.phonepe.com/apis/pg-sandbox/v1/oauth/token",
                "pg": "https://api-preprod.phonepe.com/apis/pg-sandbox"},
    "production": {"token": "https://api.phonepe.com/apis/identity-manager/v1/oauth/token",
                   "pg": "https://api.phonepe.com/apis/pg"},
}


class PaywallError(OutreachError):
    """The trial is over (or its drafts are used up) and there's no active pass."""


class PaymentError(Exception):
    pass


# ---------------------------------------------------------------- who has access
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
        "gateway": "PhonePe" if _provider() == "phonepe" else "Razorpay",
        "pass_until": aware(user.pass_until).isoformat() if _pass_active(user, at) else None,
        "trial_days": s.trial_days,
        "trial_ends": end.isoformat() if end else None,
        "trial_drafts_left": max(s.trial_drafts - (user.trial_drafts_used or 0), 0),
        "trial_chats_left": max(s.trial_chats - (user.trial_chats_used or 0), 0),
        "access": has_access(user, at),
        "plans": [{"id": k, "price": p // 100, "days": d, "label": label} for k, (p, d, label) in PLANS.items()],
    }


# ---------------------------------------------------------------- PhonePe
_token = {"value": "", "expires": 0.0}
_token_lock = threading.Lock()


def _hosts() -> dict:
    return _HOSTS["production" if get_settings().phonepe_env == "production" else "sandbox"]


def _auth_header() -> str:
    with _token_lock:
        if _token["value"] and time.time() < _token["expires"] - 120:
            return f"O-Bearer {_token['value']}"
        s = get_settings()
        r = httpx.post(_hosts()["token"], timeout=20, data={
            "client_id": s.phonepe_client_id, "client_version": s.phonepe_client_version,
            "client_secret": s.phonepe_client_secret, "grant_type": "client_credentials"})
        if r.status_code != 200:
            log.error("PhonePe token failed: %s %s", r.status_code, r.text[:300])
            raise PaymentError("Payments are unavailable right now. Please try again in a few minutes.")
        data = r.json()
        _token["value"], _token["expires"] = data["access_token"], float(data.get("expires_at") or time.time() + 600)
        return f"O-Bearer {_token['value']}"


def create_order(db: Session, user: User, plan: str) -> str:
    """Make an order with the active gateway and return the payment page to send the user to."""
    if not payments_on():
        raise PaymentError("Payments aren't switched on yet.")
    if plan not in PLANS:
        raise PaymentError("Unknown plan.")
    provider = _provider()
    amount = PLANS[plan][0]
    order_id = f"FR{user.id}-{secrets.token_hex(6)}"
    pay = Payment(user_id=user.id, email=user.email, merchant_order_id=order_id, plan=plan,
                  amount_paise=amount, state="CREATED", provider=provider)
    db.add(pay)
    db.commit()
    try:
        url = (_razorpay_create if provider == "razorpay" else _phonepe_create)(pay, user)
    except PaymentError:
        pay.state = "FAILED"
        db.commit()
        raise
    pay.state = "PENDING"
    db.commit()
    return url


def refresh(db: Session, pay: Payment) -> Payment:
    """Ask the gateway for the order's real status; turn the pass on if it's paid in full."""
    if pay.state == "COMPLETED" and pay.applied_at:
        return pay
    got = (_razorpay_status if pay.provider == "razorpay" else _phonepe_status)(pay)
    if got is None:  # gateway unreachable: try again later
        return pay
    state, paid = got
    pay.state = state
    db.commit()
    if state == "COMPLETED":
        if paid != pay.amount_paise:
            log.error("amount mismatch on %s: paid %s, expected %s", pay.merchant_order_id, paid, pay.amount_paise)
        else:
            _apply(db, pay)
    return pay


def _return_url(pay: Payment) -> str:
    return f"{get_settings().base_url.rstrip('/')}/pay/return?o={pay.merchant_order_id}"


def _phonepe_create(pay: Payment, user: User) -> str:
    body = {
        "merchantOrderId": pay.merchant_order_id,
        "amount": pay.amount_paise,
        "expireAfter": 1200,
        "metaInfo": {"udf1": str(user.id), "udf2": pay.plan},
        "paymentFlow": {"type": "PG_CHECKOUT", "message": f"FounderReach {PLANS[pay.plan][2]}",
                        "merchantUrls": {"redirectUrl": _return_url(pay)}},
    }
    r = httpx.post(f"{_hosts()['pg']}/checkout/v2/pay", json=body, timeout=20,
                   headers={"Authorization": _auth_header(), "Content-Type": "application/json"})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("redirectUrl"):
        log.error("PhonePe pay failed for %s: %s %s", pay.merchant_order_id, r.status_code, r.text[:300])
        raise PaymentError("Couldn't start the payment. Please try again in a few minutes.")
    pay.gateway_ref = data.get("orderId")
    return data["redirectUrl"]


def _phonepe_status(pay: Payment) -> tuple[str, int | None] | None:
    r = httpx.get(f"{_hosts()['pg']}/checkout/v2/order/{pay.merchant_order_id}/status", timeout=20,
                  params={"details": "false"}, headers={"Authorization": _auth_header(), "Content-Type": "application/json"})
    if r.status_code != 200:
        log.warning("PhonePe status failed for %s: %s %s", pay.merchant_order_id, r.status_code, r.text[:300])
        return None
    data = r.json()
    state = data.get("state")
    return (state if state in ("PENDING", "COMPLETED", "FAILED") else pay.state), data.get("amount")


# ---------------------------------------------------------------- Razorpay (Payment Links)
_RZP = "https://api.razorpay.com/v1"
_RZP_STATES = {"created": "PENDING", "partially_paid": "PENDING", "paid": "COMPLETED", "expired": "FAILED", "cancelled": "FAILED"}


def _rzp_auth() -> tuple[str, str]:
    s = get_settings()
    return s.razorpay_key_id, s.razorpay_key_secret


def _razorpay_create(pay: Payment, user: User) -> str:
    body = {
        "amount": pay.amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": pay.merchant_order_id,  # unique, up to 40 characters
        "description": f"FounderReach {PLANS[pay.plan][2]}",
        "customer": {"name": (user.name or "")[:100], "email": user.email},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "expire_by": int(time.time()) + 30 * 60,
        "callback_url": _return_url(pay),
        "callback_method": "get",
    }
    r = httpx.post(f"{_RZP}/payment_links", json=body, auth=_rzp_auth(), timeout=20)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("short_url"):
        log.error("Razorpay link failed for %s: %s %s", pay.merchant_order_id, r.status_code, r.text[:300])
        raise PaymentError("Couldn't start the payment. Please try again in a few minutes.")
    pay.gateway_ref = data["id"]
    return data["short_url"]


def _razorpay_status(pay: Payment) -> tuple[str, int | None] | None:
    if not pay.gateway_ref:
        return None
    r = httpx.get(f"{_RZP}/payment_links/{pay.gateway_ref}", auth=_rzp_auth(), timeout=20)
    if r.status_code != 200:
        log.warning("Razorpay status failed for %s: %s %s", pay.merchant_order_id, r.status_code, r.text[:300])
        return None
    data = r.json()
    return _RZP_STATES.get(data.get("status"), pay.state), data.get("amount_paid")


def razorpay_webhook_ok(body: bytes, signature: str | None) -> bool:
    """Razorpay signs webhooks: X-Razorpay-Signature = HMAC-SHA256(webhook secret, raw body), hex."""
    secret = get_settings().razorpay_webhook_secret
    if not (secret and signature):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def _apply(db: Session, pay: Payment) -> None:
    """Turn the pass on, exactly once per order (the return page and the webhook can both arrive)."""
    at = now()
    claimed = db.execute(update(Payment).where(Payment.id == pay.id, Payment.applied_at.is_(None))
                         .values(applied_at=at)).rowcount
    db.commit()
    if not claimed:
        db.refresh(pay)
        return
    user = db.get(User, pay.user_id) if pay.user_id else None
    if user is None:  # account deleted mid-payment: keep the record for a refund
        log.error("paid order %s has no user", pay.merchant_order_id)
        return
    days = PLANS[pay.plan][1]
    start = max(aware(user.pass_until) or at, at)  # buying early extends the current pass
    user.pass_until = start + timedelta(days=days)
    db.commit()
    db.refresh(pay)
    log.info("pass %s for user %s until %s (order %s)", pay.plan, user.id, user.pass_until, pay.merchant_order_id)


def phonepe_webhook_ok(authorization: str | None) -> bool:
    """PhonePe signs webhooks with Authorization: SHA256(username:password)."""
    s = get_settings()
    if not (s.phonepe_webhook_user and s.phonepe_webhook_password and authorization):
        return False
    expected = hashlib.sha256(f"{s.phonepe_webhook_user}:{s.phonepe_webhook_password}".encode()).hexdigest()
    got = authorization.strip().removeprefix("SHA256").strip().lower()
    return hmac.compare_digest(expected, got)
