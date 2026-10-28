"""Regression tests for the bugs found in the October 2026 bug hunt (letters match the report)."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import agent, main, scheduler  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ChatMessage, Profile, Startup, User  # noqa: E402
from app.services import discovery, matching, whatsapp  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_limits():
    main._FAILS.clear()
    main._preview_hits.clear()


@pytest.fixture
def cleanup_startups():
    """The test database is shared across files: remove the startups a test adds."""
    added = []
    yield added
    with SessionLocal() as db:
        db.query(Startup).filter(Startup.domain.in_(added)).delete(synchronize_session=False)
        db.commit()


def signup(client, email, **extra):
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123", **extra})
    return r, ({"Authorization": f"Bearer {r.json()['token']}"} if r.status_code == 200 else None)


# A: "send it" or "did they reply?" must not count as the user reporting it
@pytest.mark.parametrize("text,ok", [
    ("can you send it?", False), ("did they reply?", False), ("send", False), ("done drafting, now what?", False),
    ("haven't sent it yet", False), ("I sent it", True), ("sent it! what next?", True),
    ("they replied, call on Tuesday", True), ("Priya wrote back.", True),
])
def test_A_sent_replied_guard(text, ok):
    assert agent.user_said_so(text) is ok


# D: the visitor's IP comes from the proxy's entry, not whatever the visitor wrote
def test_D_client_ip_ignores_spoofed_entries(client):
    assert client.get("/api/public/ip", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}).json()["ip"] == "203.0.113.7"
    codes = [client.get("/api/public/preview", params={"q": "ai"},
                        headers={"X-Forwarded-For": f"10.0.{i}.{i}, 203.0.113.9"}).status_code for i in range(32)]
    assert codes.count(429) >= 1  # rotating the fake part no longer dodges the limit


# E: no AI available -> a friendly reply, and no message left unanswered
def test_E_ai_unavailable_gets_a_reply(client, monkeypatch):
    def broken():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    monkeypatch.setattr(agent, "get_llm", broken)
    _, h = signup(client, "noai@gmail.com")
    r = client.post("/api/chat", headers=h, json={"message": "find me startups"})
    assert r.status_code == 200 and r.json()["reply"] == agent.AI_UNAVAILABLE
    from app.services import profile
    monkeypatch.setattr(profile, "get_llm", broken)
    r = client.post("/api/resume", headers=h, files={"file": ("cv.txt", ("Backend engineer, python. " * 20).encode())})
    assert r.status_code == 200 and "couldn't read it right now" in r.json()["reply"]
    roles = [m["role"] for m in client.get("/api/chat", headers=h).json()]
    assert roles[-4:] == ["user", "assistant", "user", "assistant"]


# F: uptime checks use HEAD
@pytest.mark.parametrize("path", ["/", "/healthz", "/terms", "/privacy", "/refunds", "/contact"])
def test_F_head_requests(client, path):
    assert client.head(path).status_code == 200


# G: short skills count, in the public search and in matching's prefilter
def test_G_short_skills_are_kept(client, cleanup_startups):
    cleanup_startups.append("shortco.in")
    assert {"ai", "ml", "go", "ui", "ux", "qa", "c#", "c++"} <= matching._tokens("AI ML Go UI/UX QA C# C++")
    assert not {"an", "and", "the", "of", "in"} & matching._tokens("an AI and the future of work in Bengaluru")
    with SessionLocal() as db:
        db.add(Startup(name="ShortCo", domain="shortco.in", one_liner="AI agents for QA teams", stage="seed",
                       location="Bengaluru", tags=["ml"]))
        db.commit()
    names = [r["name"] for r in client.get("/api/public/preview", params={"q": "ai"}).json()]
    assert "ShortCo" in names


# I: no daily matching (AI cost) for people who went quiet or never confirmed
def test_I_daily_matches_only_for_active_confirmed_users():
    now = datetime.now(timezone.utc)
    ready = Profile(summary="Backend engineer", skills=["python"], target_roles=[], preferences={}, links={}, highlights=[])
    u = User(email="x@gmail.com", password_hash="", email_verified_at=now, created_at=now - timedelta(days=60), profile=ready)
    u.last_seen_at = now - timedelta(days=2)
    assert scheduler.wants_daily_matches(u, now)
    u.last_seen_at = now - timedelta(days=30)
    assert not scheduler.wants_daily_matches(u, now)
    u.whatsapp_last_inbound_at = now - timedelta(hours=3)  # active on WhatsApp counts
    assert scheduler.wants_daily_matches(u, now)
    u.email_verified_at = None
    assert not scheduler.wants_daily_matches(u, now)


def test_I_whatsapp_backlog_is_capped():
    with SessionLocal() as db:
        u = User(email="backlog@gmail.com", password_hash="", phone="919811112222")
        db.add(u)
        db.commit()
        for i in range(12):
            db.add(ChatMessage(user_id=u.id, role="assistant", content=f"update {i}", unread=True))
        db.commit()
        notices = whatsapp.pending_notices(db, u)
    assert len(notices) == 6 and notices[0].startswith("(+7 older updates") and notices[-1] == "update 11"


# K: a Bangalore Startup Map startup keeps its credit when it later appears in the news
def test_K_map_credit_survives_news(cleanup_startups):
    cleanup_startups.append("keepco.in")
    with SessionLocal() as db:
        discovery.upsert_startups(db, [{"name": "KeepCo", "domain": "keepco.in", "stage": "seed", "location": "Bengaluru",
                                        "source": "startup_map", "source_url": "https://www.bangalorestartupmap.com/company/keepco"}])
        discovery.upsert_startups(db, [{"name": "KeepCo", "domain": "keepco.in", "signal": "raised ₹5 Cr seed",
                                        "source": "news", "source_url": "https://inc42.com/keepco"}])
        s = db.query(Startup).filter_by(domain="keepco.in").one()
    assert s.source == "startup_map" and "bangalorestartupmap.com" in s.source_url and s.signal == "raised ₹5 Cr seed"


# J: no WhatsApp app secret -> refuse webhooks rather than trust anyone
def test_J_whatsapp_refuses_without_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", "")
    assert whatsapp.signature_ok(b'{"forged": true}', None) is False


# L: names are capped
def test_L_long_names_rejected(client):
    r, _ = signup(client, "longname@gmail.com", name="x" * 101)
    assert r.status_code == 422


# M: empty or broken PDFs get a friendly message, not a crash
@pytest.mark.parametrize("data", [b"", b"%PDF-1.4 not really a pdf" * 20])
def test_M_bad_pdfs(client, data):
    _, h = signup(client, f"pdf{len(data)}@gmail.com")
    r = client.post("/api/resume", headers=h, files={"file": ("cv.pdf", data)})
    assert r.status_code == 422 and "Couldn't read much text" in r.json()["detail"]


# N: without confirmation emails, email sign-up is closed (outside the offline demo) and says why
def test_N_email_signup_closed_without_smtp(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "anthropic")
    monkeypatch.setattr(s, "smtp_user", "")
    assert client.get("/api/auth/providers").json()["email_signup"] is False
    r, _ = signup(client, "closed@gmail.com")
    assert r.status_code == 503 and "continue with Google" in r.json()["detail"]
    monkeypatch.setattr(s, "smtp_user", "me@gmail.com")
    monkeypatch.setattr(s, "smtp_password", "app-password")
    assert client.get("/api/auth/providers").json()["email_signup"] is True


# P: the 30-second poll only asks for new messages
def test_P_chat_after(client):
    _, h = signup(client, "poll@gmail.com")
    first = client.get("/api/chat", headers=h).json()
    last = first[-1]["id"]
    assert client.get("/api/chat", headers=h, params={"after": last}).json() == []
    client.post("/api/chat", headers=h, json={"message": "hello"})
    new = client.get("/api/chat", headers=h, params={"after": last}).json()
    assert [m["role"] for m in new] == ["user", "assistant"] and all(m["id"] > last for m in new)


# Q: a stranger guessing your password can't lock you out from your own network
def test_Q_lockout_is_per_network(client):
    signup(client, "lockme@gmail.com")
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "lockme@gmail.com", "password": "wrong-pass"},
                    headers={"X-Forwarded-For": "198.51.100.66"})
    bad = client.post("/api/auth/login", json={"email": "lockme@gmail.com", "password": "password123"},
                      headers={"X-Forwarded-For": "198.51.100.66"})
    good = client.post("/api/auth/login", json={"email": "lockme@gmail.com", "password": "password123"},
                       headers={"X-Forwarded-For": "192.0.2.10"})
    assert bad.status_code == 429 and good.status_code == 200


# H: one network can't mass-create accounts (or make us email strangers)
def test_H_signup_rate_limit(client):
    codes = [signup(client, f"bulk{i}@gmail.com")[0].status_code for i in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    r = client.post("/api/auth/signup", json={"email": "elsewhere@gmail.com", "password": "password123"},
                    headers={"X-Forwarded-For": "192.0.2.55"})
    assert r.status_code == 200  # another network is unaffected


def test_S_failed_signups_dont_use_up_the_limit(client):
    for _ in range(6):  # e.g. a typo'd address that the email check rejects
        assert client.post("/api/auth/signup", json={"email": "someone@gmial.com", "password": "password123"}).status_code == 400
    assert signup(client, "someone-real@gmail.com")[0].status_code == 200
