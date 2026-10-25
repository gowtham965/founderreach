"""End-to-end flow with the fake LLM (no network). FounderReach drafts; the user sends."""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(LLM_PROVIDER="fake", DATABASE_URL=f"sqlite:///{_db}", SCHEDULER_ENABLED="false",
                  NEWS_FEEDS="")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import agent  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Outreach, Startup, User  # noqa: E402
from app.services import contacts, discovery, outreach  # noqa: E402

RESUME = ("Test User — AI engineer. Python, FastAPI, SQL, LLM apps. Built a RAG search tool used by 2,000 people. "
          "Cut API latency by 40% at Acme by adding caching and async workers. ") * 3


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def offline_contacts(monkeypatch):
    monkeypatch.setattr(contacts, "has_mx", lambda d: True)
    import httpx  # offline internet: every lookup 404s, so the waterfall falls back to name patterns
    monkeypatch.setattr(contacts, "_client", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def auth(client):
    r = client.post("/api/auth/signup", json={"email": "t@example.com", "password": "password123", "name": "Test User"})
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"email": "t@example.com", "password": "password123"})
    token = r.json()["token"]
    confirm_email(client, token)
    return {"Authorization": f"Bearer {token}"}


def confirm_email(client, session_token):
    """Click the confirmation link, as the user would."""
    from app.auth import create_token, decode_token
    link_token = create_token(decode_token(session_token), purpose="verify_email", minutes=60)
    client.get(f"/api/auth/verify?token={link_token}", follow_redirects=False)


def seed_startups():
    with SessionLocal() as db:
        discovery.upsert_startups(db, [
            {"name": "VectorLoop", "domain": "vectorloop.ai", "one_liner": "LLM evaluation platform in Python",
             "stage": "seed", "signal": "raised $3M seed", "tags": ["llm", "python"],
             "founders": [{"name": "Asha Rao", "title": "CEO"}]},
            {"name": "Ledgerly", "domain": "ledgerly.io", "one_liner": "Accounting for SMBs", "stage": "series a",
             "signal": "hiring backend engineers", "tags": ["fintech", "sql"]},
        ])


def test_auth_required(client):
    assert client.get("/api/me").status_code == 401


def test_signup_and_welcome(client):
    h = auth(client)
    msgs = client.get("/api/chat", headers=h).json()
    assert msgs[0]["role"] == "assistant" and "resume" in msgs[0]["content"]


def test_resume_builds_profile(client):
    h = auth(client)
    r = client.post("/api/resume", headers=h, files={"file": ("resume.txt", RESUME.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    assert client.get("/api/me", headers=h).json()["profile"]["ready"]


def test_preferences_saved_via_chat(client):
    h = auth(client)
    client.post("/api/chat", headers=h, json={"message": "I want remote, seed stage please"})
    assert "remote" in client.get("/api/me", headers=h).json()["profile"]["preferences"]["notes"]


def test_find_matches_and_draft(client):
    h = auth(client)
    seed_startups()
    r = client.post("/api/chat", headers=h, json={"message": "find me startups"}).json()
    items = r["cards"][0]["items"]
    assert {i["name"] for i in items} == {"VectorLoop", "Ledgerly"}
    vl = next(i for i in items if i["name"] == "VectorLoop")
    r = client.post("/api/chat", headers=h, json={"message": f"draft an email to #{vl['match_id']}"}).json()
    card = r["cards"][0]["item"]
    assert card["outreach"]["status"] == "draft"
    assert card["outreach"]["to_email"] == "asha@vectorloop.ai"  # pattern guess from founder name
    assert card["outreach"]["to_confidence"] == "guess"
    assert card["outreach"]["dm_note"]  # weak address → a LinkedIn/X note is prepared as a fallback
    assert "linkedin.com/search" in card["founders"][0]["linkedin_search"]
    test_find_matches_and_draft.match_id = vl["match_id"]


def test_style_guide_is_used():
    assert "Email style guide" in outreach.draft_system()


def test_draft_opens_prefilled_in_gmail_and_mail_app(client):
    h = auth(client)
    mid = test_find_matches_and_draft.match_id
    card = next(m for m in client.get("/api/dashboard", headers=h).json()["matches"] if m["match_id"] == mid)
    o = card["outreach"]
    gmail_url = urlparse(o["compose"]["gmail"])
    q = parse_qs(gmail_url.query)
    assert gmail_url.netloc == "mail.google.com" and q["view"] == ["cm"] and q["to"] == ["asha@vectorloop.ai"]
    assert q["su"] == [o["subject"]] and q["body"] == [o["body"]]
    mailto = o["compose"]["mailto"]
    assert mailto.startswith("mailto:asha%40vectorloop.ai?") and parse_qs(urlparse(mailto).query)["body"] == [o["body"]]


def test_no_send_endpoint_and_no_gmail_access(client):
    h = auth(client)
    assert client.post(f"/api/matches/{test_find_matches_and_draft.match_id}/send", headers=h).status_code in (404, 405)
    assert client.get("/api/gmail/connect", headers=h).status_code in (404, 405)


def test_model_cannot_mark_sent_without_user_saying_so(client):
    mid = test_find_matches_and_draft.match_id
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="t@example.com").one()
        result = agent._run_tool(db, user, "mark_sent", {"match_id": mid}, "make it shorter", [])
        assert result["ok"] is False
        assert db.query(Outreach).filter_by(match_id=mid).one().status == "draft"


def test_said_so_regex():
    assert agent.user_said_so("sent it!")
    assert agent.user_said_so("they replied, we have a call Tuesday")
    assert not agent.user_said_so("haven't sent it yet")
    assert not agent.user_said_so("make it shorter")


def test_sent_then_followup_reminder_then_replied(client):
    h = auth(client)
    mid = test_find_matches_and_draft.match_id
    r = client.post("/api/chat", headers=h, json={"message": f"done, I sent #{mid}"}).json()
    o = r["cards"][0]["item"]["outreach"]
    assert o["status"] == "sent" and o["next_followup_at"]

    # day 3: the reminder job writes a follow-up draft and leaves a notice; nothing is sent
    with SessionLocal() as db:
        row = db.query(Outreach).filter_by(match_id=mid).one()
        row.next_followup_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        assert outreach.prepare_due_followups(db) == 1
        assert outreach.prepare_due_followups(db) == 0  # not twice
    card = next(m for m in client.get("/api/dashboard", headers=h).json()["matches"] if m["match_id"] == mid)
    assert card["outreach"]["followup_draft"]
    assert parse_qs(urlparse(card["outreach"]["followup_compose"]["gmail"]).query)["su"][0].startswith("Re: ")
    notice = [m for m in client.get("/api/chat", headers=h).json() if m["unread"] and "follow-up" in m["content"]]
    assert notice and "mail.google.com" in notice[-1]["content"]

    # user sends the follow-up → next reminder at day 7
    o = client.post(f"/api/matches/{mid}/followup-sent", headers=h).json()["outreach"]
    assert o["followups_sent"] == 1 and o["followup_draft"] is None and o["next_followup_at"]

    # founder replied → reminders stop
    o = client.post(f"/api/matches/{mid}/replied", headers=h).json()["outreach"]
    assert o["status"] == "replied" and o["next_followup_at"] is None
    assert client.get("/api/dashboard", headers=h).json()["stats"]["replies"] == 1


def test_cannot_redraft_sent_email(client):
    h = auth(client)
    r = client.post(f"/api/matches/{test_find_matches_and_draft.match_id}/draft", headers=h)
    assert r.status_code == 400


def test_other_users_cannot_touch_my_matches(client):
    r = client.post("/api/auth/signup", json={"email": "other@example.com", "password": "password123"})
    h2 = {"Authorization": f"Bearer {r.json()['token']}"}
    confirm_email(client, r.json()["token"])
    for action in ("sent", "replied", "draft"):
        assert client.post(f"/api/matches/{test_find_matches_and_draft.match_id}/{action}", headers=h2).status_code == 400


def test_daily_draft_cap(client, monkeypatch):
    from app.config import get_settings
    from app.models import Match
    auth(client)
    monkeypatch.setattr(get_settings(), "max_drafts_per_day", 1)
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="t@example.com").one()
        user.drafts_day, user.drafts_used = None, None  # a new day
        ledgerly = db.query(Startup).filter_by(name="Ledgerly").one()
        m = db.query(Match).filter_by(user_id=user.id, startup_id=ledgerly.id).one()
        outreach.draft(db, user, m.id)
        with pytest.raises(outreach.OutreachError, match="draft limit"):
            outreach.draft(db, user, m.id, "make it shorter")


def test_unconfirmed_users_cannot_draft_or_see_founder_emails(client):
    from app.models import Match
    from app.services import matching
    client.post("/api/auth/signup", json={"email": "unconfirmed@example.com", "password": "password123"})
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="unconfirmed@example.com").one()
        vl = db.query(Startup).filter_by(name="VectorLoop").one()
        m = Match(user_id=user.id, startup_id=vl.id, score=80)
        db.add(m)
        db.commit()
        with pytest.raises(outreach.OutreachError, match="Confirm your email"):
            outreach.draft(db, user, m.id)
        card = matching.match_card(m)
        assert card["confirmed"] is False and card["contacts"] == []


def test_parse_json_tolerates_fences():
    from app.llm import parse_json
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! [1, 2]') == [1, 2]
