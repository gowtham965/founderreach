"""Razorpay Standard Checkout: create an order (popup options), read its status, check signatures."""
import hashlib
import hmac
import logging

import httpx

from app.config import get_settings
from app.models import Payment, User
from app.services.billing.plans import PLANS, PaymentError

log = logging.getLogger(__name__)

_RZP = "https://api.razorpay.com/v1"
_RZP_STATES = {"created": "PENDING", "attempted": "PENDING", "paid": "COMPLETED"}  # an order never "fails"


def _rzp_auth() -> tuple[str, str]:
    s = get_settings()
    return s.razorpay_key_id, s.razorpay_key_secret


def _razorpay_create(pay: Payment, user: User) -> dict:
    body = {"amount": pay.amount_paise, "currency": "INR", "receipt": pay.merchant_order_id,  # receipt: up to 40 chars
            "notes": {"user_id": str(user.id), "plan": pay.plan}}
    r = httpx.post(f"{_RZP}/orders", json=body, auth=_rzp_auth(), timeout=20)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("id"):
        log.error("Razorpay order failed for %s: %s %s", pay.merchant_order_id, r.status_code, r.text[:300])
        raise PaymentError("Payments are unavailable right now. Please try again in a few minutes." if r.status_code == 401
                           else "Couldn't start the payment. Please try again in a few minutes.")
    pay.gateway_ref = data["id"]
    base = get_settings().base_url.rstrip("/")
    return {"checkout": {
        "key": get_settings().razorpay_key_id, "order_id": data["id"], "amount": pay.amount_paise, "currency": "INR",
        "name": "FounderReach", "description": PLANS[pay.plan][2], "image": f"{base}/static/brand/icon-192.png",
        "prefill": {"name": user.name or "", "email": user.email}, "notes": {"order": pay.merchant_order_id},
        "theme": {"color": "#5b6cff"},
    }, "order": pay.merchant_order_id}


def _razorpay_status(pay: Payment) -> tuple[str, int | None] | None:
    """The order is "paid" once a payment for it is captured (auto-capture on); amount_paid says how much."""
    if not pay.gateway_ref:
        return None
    r = httpx.get(f"{_RZP}/orders/{pay.gateway_ref}", auth=_rzp_auth(), timeout=20)
    if r.status_code != 200:
        log.warning("Razorpay status failed for %s: %s %s", pay.merchant_order_id, r.status_code, r.text[:300])
        return None
    data = r.json()
    return _RZP_STATES.get(data.get("status"), pay.state), data.get("amount_paid")


def razorpay_signature_ok(order_id: str, payment_id: str, signature: str) -> bool:
    """Checkout's success handler: signature = HMAC-SHA256(key secret, order_id + "|" + payment_id), hex."""
    secret = get_settings().razorpay_key_secret
    if not (secret and order_id and payment_id and signature):
        return False
    expected = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def razorpay_webhook_ok(body: bytes, signature: str | None) -> bool:
    """Razorpay signs webhooks: X-Razorpay-Signature = HMAC-SHA256(webhook secret, raw body), hex."""
    secret = get_settings().razorpay_webhook_secret
    if not (secret and signature):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())
