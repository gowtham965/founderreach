"""LinkedIn: username → profile link in emails; LinkedIn PDF export combined with the resume."""
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("NEWS_FEEDS", "")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import agent, main  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.services import outreach, profile  # noqa: E402

RESUME = ("Asha Rao. Backend engineer. Python, Go, Postgres. Built payments reconciliation at Acme that cut "
          "manual work by 70%. linkedin.com/in/asha-from-resume ") * 4
LINKEDIN = ("Contact\nwww.linkedin.com/in/asha-rao-blr (LinkedIn)\nTop Skills\nPython\nGo\nDistributed Systems\n"
            "Asha Rao\nBackend Engineer at Acme\nBengaluru, Karnataka, India\nExperience\nAcme\nSenior Engineer\n"
            "Led the move to event-driven billing.\nPage 1 of 2\n") * 3


@pytest.mark.parametrize("given,url", [
    ("asha-rao-blr", "https://www.linkedin.com/in/asha-rao-blr"),
    ("@asha-rao-blr", "https://www.linkedin.com/in/asha-rao-blr"),
    ("https://www.linkedin.com/in/asha-rao-blr/", "https://www.linkedin.com/in/asha-rao-blr"),
    ("in.linkedin.com/in/asha-rao-blr?utm_source=share", "https://www.linkedin.com/in/asha-rao-blr"),
    ("my profile is linkedin.com/in/asha-rao-blr thanks", "https://www.linkedin.com/in/asha-rao-blr"),
    ("asha rao", None),
    ("a", None),
    ("", None),
])
def test_normalize_linkedin(given, url):
    assert profile.normalize_linkedin(given) == url


def test_detects_linkedin_export():
    assert profile.is_linkedin_export(LINKEDIN)
    assert not profile.is_linkedin_export(RESUME)  # a resume that merely links to LinkedIn


@pytest.fixture
def user():
    with SessionLocal() as db:
        u = User(email=f"li{os.urandom(4).hex()}@example.com", name="Asha Rao", password_hash="x")
        db.add(u)
        db.commit()
        yield db, u


@pytest.fixture
def prompts(monkeypatch):
    seen = []
    real = profile.get_llm

    class Spy:
        def complete_json(self, system, prompt, **kw):
            seen.append(prompt)
            return real().complete_json(system, prompt, **kw)

    monkeypatch.setattr(profile, "get_llm", lambda: Spy())
    return seen


def test_resume_then_linkedin_are_combined(user, prompts):
    db, u = user
    p, kind = profile.ingest_document(db, u, RESUME)
    assert kind == "resume" and p.resume_text == RESUME and not p.linkedin_text
    p, kind = profile.ingest_document(db, u, LINKEDIN)
    assert kind == "linkedin" and p.resume_text == RESUME and p.linkedin_text == LINKEDIN
    assert "Resume:" in prompts[-1] and "LinkedIn profile:" in prompts[-1]  # profile built from both

    p, kind = profile.ingest_document(db, u, RESUME.replace("70%", "80%"))  # a new resume replaces only the resume
    assert "80%" in p.resume_text and p.linkedin_text == LINKEDIN


def test_profile_link_comes_from_export_unless_user_set_one(user, prompts):
    db, u = user
    p, _ = profile.ingest_document(db, u, LINKEDIN)
    assert p.links["linkedin"] == "https://www.linkedin.com/in/asha-rao-blr"

    db2, u2 = user
    profile.update_profile(db2, u2, linkedin="chosen-by-me")
    p, _ = profile.ingest_document(db2, u2, LINKEDIN)
    assert p.links["linkedin"] == "https://www.linkedin.com/in/chosen-by-me"


def test_bad_username_rejected(user):
    db, u = user
    with pytest.raises(ValueError, match="linkedin.com/in/"):
        profile.update_profile(db, u, linkedin="not a username")


def test_pasted_link_in_chat_is_saved(user):
    db, u = user
    agent.respond(db, u, "here you go https://www.linkedin.com/in/asha-rao-blr/")
    db.refresh(u.profile)
    assert u.profile.links["linkedin"] == "https://www.linkedin.com/in/asha-rao-blr"


def test_saved_link_is_shown_back(user, monkeypatch):
    """So a typo gets caught: the assistant is told exactly which link was saved."""
    db, u = user
    seen = {}
    real = agent.chat.get_llm()

    class Spy:
        def chat(self, system, messages, tools, **kw):
            seen["system"], seen["last"] = system, messages[-1]["content"]
            return real.chat(system, messages, tools, **kw)

    monkeypatch.setattr(agent.chat, "get_llm", lambda: Spy())
    agent.respond(db, u, "linkedin.com/in/asha-rao-blr")
    assert 'Saved: https://www.linkedin.com/in/asha-rao-blr (tap to check it\'s you)' in seen["last"]
    assert "Note for this turn" not in seen["system"]  # per-turn notes stay out of the cached system prompt


def test_link_added_under_sign_off_once(user):
    db, u = user
    profile.update_profile(db, u, linkedin="asha-rao-blr")
    body = outreach.with_linkedin("Hi Priya,\n\n...\n\nAsha", u)
    assert body.endswith("\nhttps://www.linkedin.com/in/asha-rao-blr")
    assert outreach.with_linkedin(body, u) == body
    assert outreach.with_linkedin("see linkedin.com/in/asha-rao-blr", u) == "see linkedin.com/in/asha-rao-blr"


def test_upload_reply_names_document_and_asks_for_username():
    with TestClient(main.app) as client:
        r = client.post("/api/auth/signup", json={"email": "li-upload@example.com", "password": "password123"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        plain = RESUME.replace("linkedin.com/in/asha-from-resume", "")
        reply = client.post("/api/resume", headers=h, files={"file": ("resume.txt", plain.encode())}).json()["reply"]
        assert reply.startswith("read your resume ✅") and "LinkedIn profile link" in reply
        reply = client.post("/api/resume", headers=h, files={"file": ("Profile.txt", LINKEDIN.encode())}).json()["reply"]
        assert reply.startswith("read your LinkedIn profile ✅ (combined with your resume)")
        assert "LinkedIn profile link" not in reply  # the export carried it
        assert client.get("/api/me", headers=h).json()["linkedin"] == "https://www.linkedin.com/in/asha-rao-blr"
