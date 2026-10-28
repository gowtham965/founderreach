"""Hardening: AI cost caps (per user and site-wide), the daily chat cap, the deep health check, crash alerts."""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.agents import chat as agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.llm import usage  # noqa: E402
from app.models import LLMUsage, User, now  # noqa: E402
from app.services import alerts, outreach  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh(client):
    usage._user_spend.clear()
    usage._spend.update(day="", inr=0.0, at=0.0)
    usage._alerted["day"] = ""
    with SessionLocal() as db:
        db.query(LLMUsage).delete()
        db.commit()
    yield


def make_user(client, email, admin=False, monkeypatch=None):
    main._FAILS.clear()
    main._SIGNUPS.clear()
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123"})
    with SessionLocal() as db:
        u = db.query(User).filter_by(email=email).one()
        u.email_verified_at = now()
        db.commit()
        db.refresh(u)
        db.expunge(u)
    if admin and monkeypatch:
        monkeypatch.setattr(get_settings(), "admin_emails", email)
    return u, {"Authorization": f"Bearer {r.json()['token']}"}


def spend(user_id, inr):
    """Record AI usage worth about `inr` for a user today (gpt-6-luna input at $0.10 / 1M tokens)."""
    tokens = int(inr / usage.INR_PER_USD / 0.10 * 1_000_000)
    with SessionLocal() as db:
        db.add(LLMUsage(user_id=user_id, task="chat", model="gpt-6-luna", input_tokens=tokens, output_tokens=0))
        db.commit()


def test_calls_are_charged_to_the_user(client):
    u, _ = make_user(client, "charged@gmail.com")
    with usage.for_user(u):
        usage.record("draft_email", "gpt-6-luna", NS(input_tokens=1000, output_tokens=100))
    usage.record("extract_startups", "gpt-6-luna", NS(input_tokens=1000, output_tokens=100))  # no user: system work
    with SessionLocal() as db:
        rows = {r.task: r.user_id for r in db.query(LLMUsage)}
    assert rows == {"draft_email": u.id, "extract_startups": None}


def test_user_cap_stops_their_ai_only(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_ai_inr_per_user_per_day", 25.0)
    heavy, _ = make_user(client, "heavy@gmail.com")
    light, _ = make_user(client, "light@gmail.com")
    spend(heavy.id, 26)
    spend(light.id, 3)
    with usage.for_user(heavy), pytest.raises(usage.UserBudgetExceeded, match="today's AI limit"):
        usage.check_budget()
    with usage.for_user(light):
        usage.check_budget()  # others carry on
    usage.check_budget()  # system work (discovery) isn't capped per user


def test_admins_are_never_capped(client, monkeypatch):
    boss, _ = make_user(client, "boss-guard@gmail.com", admin=True, monkeypatch=monkeypatch)
    spend(boss.id, 500)
    with usage.for_user(boss):
        usage.check_budget()


def test_spend_counts_between_refreshes(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_ai_inr_per_user_per_day", 25.0)
    u, _ = make_user(client, "creeping@gmail.com")
    spend(u.id, 24)
    with usage.for_user(u):
        usage.check_budget()  # 24 < 25, and now cached
        usage.record("chat", "gpt-6-luna", NS(input_tokens=int(2 / usage.INR_PER_USD / 0.10 * 1_000_000), output_tokens=0))
        with pytest.raises(usage.UserBudgetExceeded):
            usage.check_budget()  # the cached total went up with the new call


def test_site_cap_pauses_ai_and_alerts_once(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "daily_ai_budget_inr", 100.0)
    sent = []
    monkeypatch.setattr(alerts, "problem", lambda what: sent.append(what))
    spend(None, 101)
    for _ in range(3):
        with pytest.raises(usage.BudgetExceeded):
            usage.check_budget()
    assert len(sent) == 1 and "Daily AI budget reached" in sent[0]


def test_capped_user_gets_a_clear_message_not_an_error(client, monkeypatch):
    _, h = make_user(client, "capped-buttons@gmail.com")

    def over(*a, **k):
        raise usage.UserBudgetExceeded(usage.UserBudgetExceeded.message)
    monkeypatch.setattr(outreach, "draft", over)
    r = client.post("/api/matches/999999/draft", headers=h)
    assert r.status_code == 429 and "today's AI limit" in r.json()["detail"]


def test_daily_chat_cap(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_chats_per_day", 2)
    _, h = make_user(client, "chatty-guard@gmail.com")
    replies = [client.post("/api/chat", headers=h, json={"message": "hi"}).json()["reply"] for _ in range(3)]
    assert "daily limit" not in replies[0] and "daily limit" not in replies[1] and "daily limit" in replies[2]


def test_health_checks_the_database(client, monkeypatch):
    assert client.get("/healthz").text == "ok"

    class Broken:
        def __enter__(self):
            raise RuntimeError("disk gone")

        def __exit__(self, *a):
            return False
    from app import db as db_module
    monkeypatch.setattr(db_module, "SessionLocal", lambda: Broken())
    assert client.get("/healthz").status_code == 503


def test_crash_alerts_are_rate_limited(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send", lambda *a, **k: sent.append(a))
    alerts._last_crash.clear()
    for _ in range(5):
        alerts.crash("POST /api/matches/{match_id}/draft", KeyError("x"))
    alerts.crash("GET /api/dashboard", ValueError("y"))
    assert len(sent) == 2
