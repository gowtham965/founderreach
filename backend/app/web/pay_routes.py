"""Passes and payments: start a payment, come back from the gateway, verify, and the gateways' webhooks.

A pass only ever turns on through billing.refresh, which asks the gateway itself whether the order is paid in full.
"""
import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import get_db
from app.models import Payment, User
from app.services import billing

router = APIRouter()
log = logging.getLogger(__name__)


class PayIn(BaseModel):
    plan: str


class RazorpayPaidIn(BaseModel):
    razorpay_order_id: str = Field(min_length=1, max_length=64)
    razorpay_payment_id: str = Field(min_length=1, max_length=64)
    razorpay_signature: str = Field(min_length=1, max_length=128)


def _refresh_quietly(db: Session, pay: Payment) -> Payment:
    try:
        return billing.refresh(db, pay)
    except (billing.PaymentError, httpx.HTTPError):
        log.exception("payment status check failed")
        return pay


def _refresh_for_webhook(db: Session, pay: Payment | None, gateway: str) -> dict:
    if pay is not None:
        try:
            billing.refresh(db, pay)
        except (billing.PaymentError, httpx.HTTPError):
            log.exception("%s status check failed (webhook)", gateway)
            raise HTTPException(503, "try again")  # the gateway retries
    return {"ok": True}


@router.post("/api/pay")
def api_pay(body: PayIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Start a payment: Razorpay gets checkout options for its popup, PhonePe a page to send the browser to."""
    try:
        return billing.create_order(db, user, body.plan)
    except billing.PaymentError as exc:
        raise HTTPException(400 if not billing.payments_on() or body.plan not in billing.PLANS else 503, str(exc)) from exc
    except httpx.HTTPError as exc:
        log.exception("payment gateway unreachable")
        raise HTTPException(503, "Couldn't reach the payment gateway. Please try again in a few minutes.") from exc


@router.get("/pay/return", include_in_schema=False)
def pay_return(o: str = ""):
    """The gateway sends the browser back here (Razorpay adds its own query fields, which we don't need: the
    status is fetched from the gateway). The app then checks the order (signed in) and shows the result."""
    return RedirectResponse(f"/?paid={o}" if billing.ORDER_ID_RE.match(o) else "/", status_code=303)


@router.post("/api/pay/{order_id}/check")
def api_pay_check(order_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pay = db.scalar(select(Payment).where(Payment.merchant_order_id == order_id, Payment.user_id == user.id))
    if pay is None:
        raise HTTPException(404, "No such payment")
    pay = _refresh_quietly(db, pay)
    db.refresh(user)
    return {"state": pay.state, "plan": pay.plan, "billing": billing.summary(user)}


@router.post("/api/pay/verify")
def api_pay_verify(body: RazorpayPaidIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Razorpay checkout's success handler posts here. A bad signature changes nothing (400). A good one is still
    only a claim: the pass turns on when Razorpay's API says the order is paid in full."""
    pay = db.scalar(select(Payment).where(Payment.gateway_ref == body.razorpay_order_id, Payment.user_id == user.id,
                                          Payment.provider == "razorpay"))
    if pay is None:
        raise HTTPException(404, "No such payment")
    if not billing.razorpay_signature_ok(body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        log.warning("bad Razorpay signature for %s", pay.merchant_order_id)
        raise HTTPException(400, "We couldn't verify that payment. If money left your account, write to us from the Contact page.")
    pay = _refresh_quietly(db, pay)
    db.refresh(user)
    return {"state": pay.state, "order": pay.merchant_order_id, "billing": billing.summary(user)}


@router.post("/api/pay/webhook", include_in_schema=False)
async def pay_webhook(request: Request, db: Session = Depends(get_db)):
    """PhonePe's server-to-server notice. The body is only a hint: the status comes from PhonePe's API."""
    if not billing.phonepe_webhook_ok(request.headers.get("authorization")):
        raise HTTPException(401, "bad signature")
    try:
        data = json.loads(await request.body())
    except ValueError:
        raise HTTPException(400, "bad json")
    order_id = str(((data.get("payload") or {}) if isinstance(data, dict) else {}).get("merchantOrderId", ""))
    pay = db.scalar(select(Payment).where(Payment.merchant_order_id == order_id)) if billing.ORDER_ID_RE.match(order_id) else None
    return _refresh_for_webhook(db, pay, "PhonePe")


@router.post("/api/pay/razorpay-webhook", include_in_schema=False)
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Razorpay's server-to-server notice (order.paid, payment.captured, payment.failed). Signed over the raw body;
    the status itself is then fetched from Razorpay's API, so a pass turns on even if the browser tab was closed."""
    body = await request.body()
    if not billing.razorpay_webhook_ok(body, request.headers.get("x-razorpay-signature")):
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(body).get("payload") or {}
        order_id = ((payload.get("order") or {}).get("entity") or {}).get("id") \
            or ((payload.get("payment") or {}).get("entity") or {}).get("order_id")
    except (ValueError, AttributeError):
        return {"ok": True}
    pay = db.scalar(select(Payment).where(Payment.gateway_ref == str(order_id), Payment.provider == "razorpay")) if order_id else None
    return _refresh_for_webhook(db, pay, "Razorpay")
