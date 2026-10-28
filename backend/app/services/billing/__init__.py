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
import httpx  # noqa: F401  (tests patch billing.httpx.post/get: the same httpx module the gateways use)

from app.services.billing.access import (_pass_active, _provider, has_access, payments_on, require_access,  # noqa: F401
                                         summary, use_chat, use_draft)
from app.services.billing.orders import _apply, create_order, refresh  # noqa: F401
from app.services.billing.phonepe import _token, phonepe_webhook_ok  # noqa: F401
from app.services.billing.plans import ORDER_ID_RE, PLANS, PaymentError, PaywallError, price_line  # noqa: F401
from app.services.billing.razorpay import razorpay_signature_ok, razorpay_webhook_ok  # noqa: F401
