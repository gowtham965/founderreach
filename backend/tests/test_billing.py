"""Trial, passes and PhonePe payments, against a fake PhonePe."""
import hashlib
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main, scheduler  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Payment, Profile, User, aware, now  # noqa: E402
from app.services import billing  # noqa: E402


class FakePhonePe:
    """Records calls; answers like PhonePe's sandbox."""

    def __init__(self):
        self.state, self.amount, self.calls = "PENDING", None, []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_at": 9999999999, "token_type": "O-Bearer"})
        body = kw["json"]
        self.amount = body["amount"] if self.amount is None else self.amount
        return httpx.Response(200, json={"orderId": "OMO1", "state": "PENDING", "expireAt": 1,
                                         "redirectUrl": "https://mercury-uat.phonepe.com/transact/uat_v2?token=x"})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return httpx.Response(200, json={"orderId": "OMO1", "state": self.state, "amount": self.amount})


@pytest.fixture
def phonepe(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "payment_provider", "phonepe")
    monkeypatch.setattr(s, "phonepe_client_id", "cid")
    monkeypatch.setattr(s, "phonepe_client_secret", "secret")
    monkeypatch.setattr(s, "phonepe_webhook_user", "hookuser")
    monkeypatch.setattr(s, "phonepe_webhook_password", "hookpass")
    monkeypatch.setattr(s, "admin_emails", "")
    billing._token.update(value="", expires=0.0)
    fake = FakePhonePe()
    monkeypatch.setattr(billing.httpx, "post", fake.post)
    monkeypatch.setattr(billing.httpx, "get", fake.get)
    return fake


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_limits():
    main._FAILS.clear()
    main._SIGNUPS.clear()


def signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    with SessionLocal() as db:
        u = db.query(User).filter_by(email=email).one()
        u.email_verified_at = now()
        db.commit()
        return h, u.id


def user(uid):
    with SessionLocal() as db:
        u = db.get(User, uid)
        db.expunge(u)
        return u


def test_everything_open_while_payments_are_off(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "payment_provider", "razorpay")
    monkeypatch.setattr(get_settings(), "razorpay_key_id", "")
    h, uid = signup(client, "free@gmail.com")
    b = client.get("/api/me", headers=h).json()["billing"]
    assert b["payments"] is False and b["access"] is True
    assert client.post("/api/pay", headers=h, json={"plan": "week"}).status_code == 400


def test_trial_starts_at_first_use_and_caps_drafts(client, phonepe):
    h, uid = signup(client, "trial@gmail.com")
    assert user(uid).trial_started_at is None  # signing up doesn't start the clock
    with SessionLocal() as db:
        u = db.get(User, uid)
        for _ in range(get_settings().trial_drafts):
            billing.use_draft(db, u)
        assert u.trial_started_at is not None
        with pytest.raises(billing.PaywallError, match="free drafts"):
            billing.use_draft(db, u)
        assert billing.has_access(u)  # matches still allowed until the trial days end
        u.trial_started_at = now() - timedelta(days=get_settings().trial_days, minutes=1)
        db.commit()
        assert not billing.has_access(u)
        with pytest.raises(billing.PaywallError, match="trial has ended"):
            billing.require_access(db, u)


def test_paywall_is_402_on_draft_buttons(client, phonepe):
    h, uid = signup(client, "locked@gmail.com")
    with SessionLocal() as db:
        u = db.get(User, uid)
        u.trial_started_at = now() - timedelta(days=30)
        u.profile = Profile(summary="Backend engineer", skills=["python"], target_roles=[], preferences={}, links={}, highlights=[])
        db.commit()
        from app.models import Match, Startup
        s = Startup(name="PayCo", domain="payco-test.in", stage="seed", location="Bengaluru")
        db.add(s)
        db.commit()
        m = Match(user_id=uid, startup_id=s.id, score=80, reason="fits")
        db.add(m)
        db.commit()
        mid = m.id
    r = client.post(f"/api/matches/{mid}/draft", headers=h)
    assert r.status_code == 402 and "Get a pass" in r.json()["detail"]
    from app.models import Match, Startup
    with SessionLocal() as db:
        assert not scheduler.wants_daily_matches(db.get(User, uid))  # no AI spend on locked accounts
        db.query(Match).filter_by(user_id=uid).delete()  # the test database is shared across files
        db.query(Startup).filter_by(domain="payco-test.in").delete()
        db.commit()


def test_pay_then_return_turns_on_a_pass_once(client, phonepe):
    h, uid = signup(client, "payer@gmail.com")
    r = client.post("/api/pay", headers=h, json={"plan": "week"})
    assert r.status_code == 200 and r.json()["redirect_url"].startswith("https://mercury-uat.phonepe.com/")
    pay_call = next(c for c in phonepe.calls if c[1].endswith("/checkout/v2/pay"))
    assert pay_call[2]["headers"]["Authorization"] == "O-Bearer tok" and pay_call[2]["json"]["amount"] == 29900
    with SessionLocal() as db:
        order = db.query(Payment).filter_by(user_id=uid).one().merchant_order_id
    redirect = pay_call[2]["json"]["paymentFlow"]["merchantUrls"]["redirectUrl"]
    assert redirect.endswith(f"/pay/return?o={order}")

    back = client.get(f"/pay/return?o={order}", follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == f"/?paid={order}"
    assert client.get("/pay/return?o=<script>", follow_redirects=False).headers["location"] == "/"

    pending = client.post(f"/api/pay/{order}/check", headers=h).json()
    assert pending["state"] == "PENDING" and pending["billing"]["pass_until"] is None

    phonepe.state = "COMPLETED"
    done = client.post(f"/api/pay/{order}/check", headers=h).json()
    assert done["state"] == "COMPLETED" and done["billing"]["pass_until"]
    until = aware(user(uid).pass_until)
    assert timedelta(days=6, hours=23) < until - now() <= timedelta(days=7)

    client.post(f"/api/pay/{order}/check", headers=h)  # checking again doesn't add another week
    assert aware(user(uid).pass_until) == until
    assert client.post(f"/api/pay/{order}/check", headers=signup(client, "other@gmail.com")[0]).status_code == 404


def test_second_pass_extends_the_first(client, phonepe):
    h, uid = signup(client, "extend@gmail.com")
    with SessionLocal() as db:
        db.get(User, uid).pass_until = now() + timedelta(days=3)
        db.commit()
    client.post("/api/pay", headers=h, json={"plan": "month"})
    phonepe.state = "COMPLETED"
    with SessionLocal() as db:
        order = db.query(Payment).filter_by(user_id=uid).one().merchant_order_id
    client.post(f"/api/pay/{order}/check", headers=h)
    left = aware(user(uid).pass_until) - now()
    assert timedelta(days=32, hours=23) < left <= timedelta(days=33)


def test_wrong_amount_never_turns_on_a_pass(client, phonepe):
    h, uid = signup(client, "short@gmail.com")
    client.post("/api/pay", headers=h, json={"plan": "month"})
    phonepe.state, phonepe.amount = "COMPLETED", 100
    with SessionLocal() as db:
        order = db.query(Payment).filter_by(user_id=uid).one().merchant_order_id
    client.post(f"/api/pay/{order}/check", headers=h)
    assert user(uid).pass_until is None


def test_webhook_needs_phonepe_signature_and_checks_status_itself(client, phonepe):
    h, uid = signup(client, "hooked@gmail.com")
    client.post("/api/pay", headers=h, json={"plan": "week"})
    with SessionLocal() as db:
        order = db.query(Payment).filter_by(user_id=uid).one().merchant_order_id
    body = {"event": "checkout.order.completed", "payload": {"merchantOrderId": order, "state": "COMPLETED"}}
    assert client.post("/api/pay/webhook", json=body).status_code == 401
    assert client.post("/api/pay/webhook", json=body, headers={"Authorization": "nope"}).status_code == 401
    good = {"Authorization": hashlib.sha256(b"hookuser:hookpass").hexdigest()}
    # the payload says COMPLETED, but PhonePe's status API says PENDING: no pass
    assert client.post("/api/pay/webhook", json=body, headers=good).status_code == 200
    assert user(uid).pass_until is None
    phonepe.state = "COMPLETED"
    assert client.post("/api/pay/webhook", json=body, headers=good).status_code == 200
    assert user(uid).pass_until is not None


def test_webhook_refused_without_configured_credentials(monkeypatch):
    monkeypatch.setattr(get_settings(), "phonepe_webhook_user", "")
    assert billing.phonepe_webhook_ok(hashlib.sha256(b":").hexdigest()) is False


def test_deleting_account_keeps_payment_record(client, phonepe):
    h, uid = signup(client, "leaver@gmail.com")
    client.post("/api/pay", headers=h, json={"plan": "week"})
    assert client.delete("/api/me", headers=h).status_code == 200
    with SessionLocal() as db:
        p = db.query(Payment).filter_by(email="leaver@gmail.com").one()
        assert p.user_id is None


def test_morning_matches_start_the_trial(client, phonepe):
    _, uid = signup(client, "morning@gmail.com")
    with SessionLocal() as db:
        u = db.get(User, uid)
        u.profile = Profile(summary="Backend engineer", skills=["python"], target_roles=[], preferences={}, links={}, highlights=[])
        u.last_seen_at = now()
        db.commit()
    scheduler.job_daily_matches()
    assert user(uid).trial_started_at is not None


def test_trial_chat_cap_answers_with_the_offer(client, phonepe, monkeypatch):
    monkeypatch.setattr(get_settings(), "trial_chats", 2)
    h, uid = signup(client, "chatty@gmail.com")
    for _ in range(2):
        r = client.post("/api/chat", headers=h, json={"message": "hello"}).json()
        assert not r.get("paywall")
    r = client.post("/api/chat", headers=h, json={"message": "hello again"}).json()
    assert r["paywall"] is True and "2 free chat messages" in r["reply"] and "₹299 for 7 days or ₹699 for 30 days" in r["reply"]
    roles = [m["role"] for m in client.get("/api/chat", headers=h).json()][-2:]
    assert roles == ["user", "assistant"]  # the message and the offer are both in the history


def test_prices_come_from_one_place():
    from app import pages
    assert billing.price_line() == "₹299 for 7 days or ₹699 for 30 days"
    assert billing.price_line() in pages.terms() and billing.price_line() in pages.refunds()


# ---------------------------------------------------------------- Razorpay
class FakeRazorpay:
    def __init__(self):
        self.status, self.paid, self.calls = "created", 0, []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return httpx.Response(200, json={"id": "plink_TEST1", "short_url": "https://rzp.io/rzp/abc", "status": "created"})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return httpx.Response(200, json={"id": "plink_TEST1", "status": self.status, "amount_paid": self.paid})


@pytest.fixture
def razorpay(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "payment_provider", "razorpay")
    monkeypatch.setattr(s, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(s, "razorpay_key_secret", "keysecret")
    monkeypatch.setattr(s, "razorpay_webhook_secret", "hooksecret")
    monkeypatch.setattr(s, "admin_emails", "")
    fake = FakeRazorpay()
    monkeypatch.setattr(billing.httpx, "post", fake.post)
    monkeypatch.setattr(billing.httpx, "get", fake.get)
    return fake


def test_razorpay_link_then_return_turns_on_a_pass(client, razorpay):
    h, uid = signup(client, "rzp@gmail.com")
    assert client.get("/api/me", headers=h).json()["billing"]["gateway"] == "Razorpay"
    r = client.post("/api/pay", headers=h, json={"plan": "month"})
    assert r.status_code == 200 and r.json()["redirect_url"] == "https://rzp.io/rzp/abc"
    _, url, kw = razorpay.calls[0]
    assert url.endswith("/v1/payment_links") and kw["auth"] == ("rzp_test_key", "keysecret")
    body = kw["json"]
    with SessionLocal() as db:
        pay = db.query(Payment).filter_by(user_id=uid).one()
        assert (pay.provider, pay.gateway_ref, pay.state) == ("razorpay", "plink_TEST1", "PENDING")
    assert body["amount"] == 69900 and body["reference_id"] == pay.merchant_order_id and len(body["reference_id"]) <= 40
    assert body["callback_url"].endswith(f"/pay/return?o={pay.merchant_order_id}") and body["callback_method"] == "get"
    assert body["accept_partial"] is False and body["notify"] == {"sms": False, "email": False}

    order = pay.merchant_order_id
    back = client.get(f"/pay/return?o={order}&razorpay_payment_id=pay_1&razorpay_payment_link_status=paid", follow_redirects=False)
    assert back.headers["location"] == f"/?paid={order}"
    assert client.post(f"/api/pay/{order}/check", headers=h).json()["state"] == "PENDING"
    razorpay.status, razorpay.paid = "paid", 69900
    done = client.post(f"/api/pay/{order}/check", headers=h).json()
    assert done["state"] == "COMPLETED" and done["billing"]["pass_until"]
    left = aware(user(uid).pass_until) - now()
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)


def test_razorpay_partial_or_short_payment_never_turns_on_a_pass(client, razorpay):
    h, uid = signup(client, "rzp-short@gmail.com")
    client.post("/api/pay", headers=h, json={"plan": "week"})
    with SessionLocal() as db:
        order = db.query(Payment).filter_by(user_id=uid).one().merchant_order_id
    razorpay.status, razorpay.paid = "partially_paid", 10000
    assert client.post(f"/api/pay/{order}/check", headers=h).json()["state"] == "PENDING"
    razorpay.status = "paid"  # says paid, but not the full amount
    client.post(f"/api/pay/{order}/check", headers=h)
    assert user(uid).pass_until is None
    razorpay.status, razorpay.paid = "expired", 0
    assert client.post(f"/api/pay/{order}/check", headers=h).json()["state"] == "FAILED"


def test_razorpay_webhook_signature_and_status_check(client, razorpay):
    import hmac as _hmac
    import json as _json
    h, uid = signup(client, "rzp-hook@gmail.com")
    client.post("/api/pay", headers=h, json={"plan": "week"})
    with SessionLocal() as db:
        order = db.query(Payment).filter_by(user_id=uid).one().merchant_order_id
    raw = _json.dumps({"event": "payment_link.paid", "payload": {"payment_link": {"entity": {
        "id": "plink_TEST1", "reference_id": order, "status": "paid"}}}}).encode()
    sig = _hmac.new(b"hooksecret", raw, hashlib.sha256).hexdigest()
    url = "/api/pay/razorpay-webhook"
    assert client.post(url, content=raw).status_code == 401
    assert client.post(url, content=raw, headers={"X-Razorpay-Signature": "0" * 64}).status_code == 401
    # signed, says paid, but Razorpay's API still says created: no pass
    assert client.post(url, content=raw, headers={"X-Razorpay-Signature": sig}).status_code == 200
    assert user(uid).pass_until is None
    razorpay.status, razorpay.paid = "paid", 29900
    assert client.post(url, content=raw, headers={"X-Razorpay-Signature": sig}).status_code == 200
    assert user(uid).pass_until is not None
    other = b'{"event": "refund.processed", "payload": {}}'
    assert client.post(url, content=other, headers={"X-Razorpay-Signature": _hmac.new(b"hooksecret", other, hashlib.sha256).hexdigest()}).status_code == 200


def test_payments_follow_the_chosen_gateways_keys(monkeypatch):
    s = get_settings()
    for k in ("razorpay_key_id", "razorpay_key_secret", "phonepe_client_id", "phonepe_client_secret"):
        monkeypatch.setattr(s, k, "")
    monkeypatch.setattr(s, "payment_provider", "razorpay")
    monkeypatch.setattr(s, "phonepe_client_id", "cid")
    monkeypatch.setattr(s, "phonepe_client_secret", "sec")
    assert billing.payments_on() is False  # PhonePe keys don't switch on a Razorpay setup
    monkeypatch.setattr(s, "payment_provider", "phonepe")
    assert billing.payments_on() is True
