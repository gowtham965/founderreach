"""Admin tab data and ntfy alerts."""
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main, scheduler  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ChatMessage, Payment, User, now  # noqa: E402
from app.services import alerts, billing  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    """Capture alerts instead of calling ntfy."""
    out = []
    monkeypatch.setattr(get_settings(), "ntfy_topic", "fr-test-topic")
    monkeypatch.setattr(get_settings(), "base_url", "https://founderreach.in")  # alerts only go out from the live site
    monkeypatch.setattr(alerts, "_post", lambda title, message, tags, priority: out.append((title, message, tags)))
    monkeypatch.setattr(alerts.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    return out


def signup(client, email):
    main._FAILS.clear()
    main._SIGNUPS.clear()
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123"})
    with SessionLocal() as db:
        db.query(User).filter_by(email=email).one().email_verified_at = now()
        db.commit()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_mask_hides_most_of_the_email():
    assert alerts.mask("priya.sharma@gmail.com") == "pr***@gmail.com" and alerts.mask("") == "***"


def test_no_topic_no_alert(monkeypatch):
    monkeypatch.setattr(get_settings(), "ntfy_topic", "")
    called = []
    monkeypatch.setattr(alerts, "_post", lambda *a: called.append(a))
    assert alerts.send("x", "y") is False and called == []


def test_signup_payment_and_failure_alert(client, sent):
    signup(client, "newperson@gmail.com")
    assert sent[-1][0] == "New sign-up" and "ne***@gmail.com" in sent[-1][1] and "newperson" not in sent[-1][1]
    with SessionLocal() as db:
        u = db.query(User).filter_by(email="newperson@gmail.com").one()
        pay = Payment(user_id=u.id, email=u.email, merchant_order_id="FR9-00000000abcd", plan="month", amount_paise=69900, state="COMPLETED")
        db.add(pay)
        db.commit()
        billing._apply(db, pay)
    assert sent[-1][0] == "Payment received" and "Rs 699" in sent[-1][1] and "30-day pass" in sent[-1][1]
    scheduler.record("discovery", False, {"error": "feeds down"})
    assert sent[-1][0] == "FounderReach needs a look" and "discovery" in sent[-1][1]


def test_alerts_never_break_a_signup(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "ntfy_topic", "fr-test-topic")
    monkeypatch.setattr(get_settings(), "base_url", "https://founderreach.in")
    monkeypatch.setattr(alerts.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ntfy down")))
    alerts.send("x", "y", wait=True)  # logs, doesn't raise


def test_admin_overview(client, monkeypatch, sent):
    boss = signup(client, "boss-admin@gmail.com")
    user = signup(client, "plain-user@gmail.com")
    monkeypatch.setattr(get_settings(), "admin_emails", "boss-admin@gmail.com")
    assert client.get("/api/admin/overview", headers=user).status_code == 403
    assert client.post("/api/admin/test-alert", headers=user).status_code == 403
    with SessionLocal() as db:
        u = db.query(User).filter_by(email="plain-user@gmail.com").one()
        db.add(ChatMessage(user_id=u.id, role="user", content="hi"))
        db.commit()
    d = client.get("/api/admin/overview", headers=boss).json()
    assert d["windows"]["today"]["signups"] >= 2 and d["windows"]["today"]["active"] >= 1
    assert len(d["series"]) == 30 and d["series"][-1]["signups"] >= 2
    assert [s["step"] for s in d["funnel"]][0] == "Signed up" and d["funnel"][0]["users"] >= d["funnel"][-1]["users"]
    row = next(r for r in d["users"] if r["email"] == "plain-user@gmail.com")
    assert row["via"] == "Email" and row["status"] == "not started" and row["confirmed"]
    assert next(r for r in d["users"] if r["email"] == "boss-admin@gmail.com")["status"] == "admin"
    only = client.get("/api/admin/overview", headers=boss, params={"q": "plain-user"}).json()["users"]
    assert [r["email"] for r in only] == ["plain-user@gmail.com"]
    assert client.post("/api/admin/test-alert", headers=boss).json() == {"ok": True}
    assert sent[-1][0] == "FounderReach test alert"


def test_local_copies_never_send_alerts(monkeypatch):
    """A dev or test server with the topic in its .env must not page the admin about its own test sign-ups."""
    called = []
    monkeypatch.setattr(get_settings(), "ntfy_topic", "fr-test-topic")
    monkeypatch.setattr(alerts, "_post", lambda *a: called.append(a))
    for local in ("http://localhost:8000", "http://127.0.0.1:8000", ""):
        monkeypatch.setattr(get_settings(), "base_url", local)
        assert alerts.send("New sign-up", "x", wait=True) is False
    assert called == []
