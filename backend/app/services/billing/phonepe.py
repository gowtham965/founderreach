"""PhonePe Standard Checkout v2: OAuth token, create a payment page, read an order's status, verify webhooks."""
import hashlib
import hmac
import logging
import threading
import time

import httpx

from app.config import get_settings
from app.models import Payment, User
from app.services.billing.plans import PLANS, PaymentError

log = logging.getLogger(__name__)

_HOSTS = {
    "sandbox": {"token": "https://api-preprod.phonepe.com/apis/pg-sandbox/v1/oauth/token",
                "pg": "https://api-preprod.phonepe.com/apis/pg-sandbox"},
    "production": {"token": "https://api.phonepe.com/apis/identity-manager/v1/oauth/token",
                   "pg": "https://api.phonepe.com/apis/pg"},
}

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


def phonepe_webhook_ok(authorization: str | None) -> bool:
    """PhonePe signs webhooks with Authorization: SHA256(username:password)."""
    s = get_settings()
    if not (s.phonepe_webhook_user and s.phonepe_webhook_password and authorization):
        return False
    expected = hashlib.sha256(f"{s.phonepe_webhook_user}:{s.phonepe_webhook_password}".encode()).hexdigest()
    got = authorization.strip().removeprefix("SHA256").strip().lower()
    return hmac.compare_digest(expected, got)
