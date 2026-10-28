"""Proof-of-work agent: ideas → starter → link added to the email."""
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Match, Profile, Startup, User, now  # noqa: E402
from app.services import contacts, outreach  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def match(client, monkeypatch):
    main._FAILS.clear()
    main._SIGNUPS.clear()
    monkeypatch.setattr(contacts, "_get", lambda url, **kw: None)  # no network: no site, no research
    monkeypatch.setattr(outreach.enrich, "enrich_startup", lambda db, s, force=False: s)
    r = client.post("/api/auth/signup", json={"email": f"pow{os.urandom(3).hex()}@gmail.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    with SessionLocal() as db:
        u = db.query(User).order_by(User.id.desc()).first()
        u.email_verified_at = now()
        u.profile = Profile(summary="Backend engineer", skills=["python"], target_roles=[], preferences={}, links={}, highlights=[])
        s = Startup(name="PowCo", domain=f"powco{os.urandom(2).hex()}.in", stage="seed", location="Bengaluru",
                    contacts=[{"email": "ceo@powco.in", "name": "Asha", "confidence": "public"}])
        db.add(s)
        db.commit()
        m = Match(user_id=u.id, startup_id=s.id, score=80, reason="fits")
        db.add(m)
        db.commit()
        yield h, m.id, s.id
        db.query(Match).filter_by(id=m.id).delete()
        db.query(Startup).filter_by(id=s.id).delete()
        db.commit()


def test_ideas_then_starter_then_link_goes_into_the_email(client, match):
    h, mid, _ = match
    card = client.post(f"/api/matches/{mid}/pow", headers=h).json()
    assert [i["title"] for i in card["pow"]["ideas"]] == ["Idea 1", "Idea 2", "Idea 3"]

    assert client.post(f"/api/matches/{mid}/pow-link", headers=h, json={"link": "https://docs.google.com/x"}).status_code == 400  # pick first
    card = client.post(f"/api/matches/{mid}/pow/1", headers=h).json()
    assert card["pow"]["chosen"] == 1 and "Check before sharing" in card["pow"]["starter"]
    assert client.post(f"/api/matches/{mid}/pow/7", headers=h).status_code == 400

    for bad in ("not a link", "http://insecure.example.com/doc", "javascript:alert(1)//x.com"):
        assert client.post(f"/api/matches/{mid}/pow-link", headers=h, json={"link": bad}).status_code in (400, 422)
    card = client.post(f"/api/matches/{mid}/pow-link", headers=h, json={"link": "https://www.loom.com/share/abc123"}).json()
    assert card["pow"]["link"] == "https://www.loom.com/share/abc123"
    assert card["outreach"]["status"] == "draft" and card["outreach"]["body"]  # the email was (re)drafted


def test_someone_elses_match_is_off_limits(client, match):
    _, mid, _ = match
    r = client.post("/api/auth/signup", json={"email": f"pow-other{os.urandom(3).hex()}@gmail.com", "password": "password123"})
    other = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post(f"/api/matches/{mid}/pow", headers=other).status_code == 400


def test_chat_can_suggest_proof_of_work(client, match, monkeypatch):
    from app import agent
    h, mid, _ = match
    with SessionLocal() as db:
        u = db.get(Match, mid).user
        cards: list = []
        out = agent._run_tool(db, u, "proof_of_work", {"match_id": mid}, "what could I make for them?", cards)
    assert out["ok"] and len(out["ideas"]) == 3 and cards and cards[0]["item"]["pow"]
