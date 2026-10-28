"""Orders: start one with the active gateway, ask the gateway for its real status, and turn the pass on once."""
import logging
import secrets
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Payment, User, aware, now
from app.services.billing import phonepe, razorpay
from app.services.billing.access import _provider, payments_on
from app.services.billing.plans import PLANS, PaymentError

log = logging.getLogger(__name__)


def create_order(db: Session, user: User, plan: str) -> dict:
    """Make an order with the active gateway. Returns {"redirect_url"} (PhonePe: send the browser there) or
    {"checkout"} (Razorpay: options for the in-page checkout popup; the key id is public, the secret never leaves)."""
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
        out = razorpay._razorpay_create(pay, user) if provider == "razorpay" else {"redirect_url": phonepe._phonepe_create(pay, user)}
    except PaymentError:
        pay.state = "FAILED"
        db.commit()
        raise
    pay.state = "PENDING"
    db.commit()
    return out


def refresh(db: Session, pay: Payment) -> Payment:
    """Ask the gateway for the order's real status; turn the pass on if it's paid in full."""
    if pay.state == "COMPLETED" and pay.applied_at:
        return pay
    got = (razorpay._razorpay_status if pay.provider == "razorpay" else phonepe._phonepe_status)(pay)
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
    from app.services import alerts
    alerts.payment(user.email, PLANS[pay.plan][2], pay.amount_paise // 100)
