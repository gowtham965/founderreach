"""Passes and their prices, order ids, and the two errors billing raises."""
import re

from app.services.outreach import OutreachError

# plan -> (price in paise, days, label)
PLANS = {
    "week": (29900, 7, "7-day pass"),
    "month": (69900, 30, "30-day pass"),
}


def price_line() -> str:
    """"₹299 for 7 days or ₹699 for 30 days": used in messages and the policy pages, so prices live only above."""
    return " or ".join(f"₹{p // 100} for {d} days" for p, d, _ in PLANS.values())
ORDER_ID_RE = re.compile(r"^FR\d+-[0-9a-f]{12}$")


class PaywallError(OutreachError):
    """The trial is over (or its drafts are used up) and there's no active pass."""


class PaymentError(Exception):
    pass
