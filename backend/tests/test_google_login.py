"""Sign in with Google. Only the network call to Google is faked."""
import os
from datetime import datetime, timezone
import tempfile
from urllib.parse import parse_qs, urlparse

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("NEWS_FEEDS", "")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ChatMessage, User  # noqa: E402
from app.services import google_login  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def google_configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "google_client_id", "test-client-id")
    monkeypatch.setattr(s, "google_client_secret", "test-secret")


def fake_google(monkeypatch, **identity):
    ident = {"sub": "g-1", "email": "g@example.com", "email_verified": True, "name": "Gee User"} | identity
    monkeypatch.setattr(google_login, "fetch_identity", lambda code: ident)


def start(client):
    """Click 'Continue with Google'; return the state Google would echo back."""
    r = client.get("/api/auth/google", follow_redirects=False)
    assert r.status_code in (302, 307)
    url = urlparse(r.headers["location"])
    assert url.netloc == "accounts.google.com"
    q = parse_qs(url.query)
    assert q["scope"][0] == "openid email profile"  # no Gmail scopes at sign-in
    return q["state"][0]


def finish(client, state, code="abc"):
    return client.get(f"/api/auth/google/callback?code={code}&state={state}", follow_redirects=False)


def token_from(r):
    loc = r.headers["location"]
    assert loc.startswith("/#token="), loc
    return loc.removeprefix("/#token=")


def test_providers_reports_google_when_configured(client):
    assert client.get("/api/auth/providers").json()["google"] is True


def test_providers_hides_google_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "google_client_id", "")
    assert client.get("/api/auth/providers").json()["google"] is False


def test_new_google_user_gets_account_and_welcome(client, monkeypatch):
    fake_google(monkeypatch, sub="g-new", email="New.Person@Example.com", name="New Person")
    r = finish(client, start(client))
    token = token_from(r)
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == "new.person@example.com"
    assert me["name"] == "New Person"
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="new.person@example.com").one()
        assert user.google_sub == "g-new"
        assert db.query(ChatMessage).filter_by(user_id=user.id).count() == 1


def test_confirmed_password_account_is_linked_and_keeps_password(client, monkeypatch):
    client.post("/api/auth/signup", json={"email": "both@example.com", "password": "password123", "name": "Both"})
    with SessionLocal() as db:  # they proved they own the address
        db.query(User).filter_by(email="both@example.com").one().email_verified_at = datetime.now(timezone.utc)
        db.commit()
    fake_google(monkeypatch, sub="g-both", email="both@example.com")
    token_from(finish(client, start(client)))
    with SessionLocal() as db:
        users = db.query(User).filter_by(email="both@example.com").all()
        assert len(users) == 1 and users[0].google_sub == "g-both"
    assert client.post("/api/auth/login", json={"email": "both@example.com", "password": "password123"}).status_code == 200


def test_google_takes_over_an_unconfirmed_account_from_whoever_registered_it(client, monkeypatch):
    """Pre-account hijacking: an attacker registers the victim's email with their own password.
    When the real owner signs in with Google, the attacker's password and sessions stop working."""
    r = client.post("/api/auth/signup", json={"email": "squat@example.com", "password": "attacker123"})
    attacker = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/me", headers=attacker).status_code == 200
    fake_google(monkeypatch, sub="g-squat", email="squat@example.com")
    owner = token_from(finish(client, start(client)))
    assert client.get("/api/me", headers=attacker).status_code == 401  # signed out
    assert client.post("/api/auth/login", json={"email": "squat@example.com", "password": "attacker123"}).status_code == 401
    me = client.get("/api/me", headers={"Authorization": f"Bearer {owner}"}).json()
    assert me["email"] == "squat@example.com" and me["email_verified"] is True


def test_returning_google_user_found_by_google_id_even_if_email_changed(client, monkeypatch):
    fake_google(monkeypatch, sub="g-ret", email="old@example.com")
    first = token_from(finish(client, start(client)))
    fake_google(monkeypatch, sub="g-ret", email="renamed@example.com")
    second = token_from(finish(client, start(client)))
    h1 = client.get("/api/me", headers={"Authorization": f"Bearer {first}"}).json()
    h2 = client.get("/api/me", headers={"Authorization": f"Bearer {second}"}).json()
    assert h1["email"] == h2["email"] == "old@example.com"


def test_unverified_google_email_is_refused(client, monkeypatch):
    fake_google(monkeypatch, sub="g-unv", email="unverified@example.com", email_verified=False)
    r = finish(client, start(client))
    assert r.headers["location"] == "/?google=error"
    with SessionLocal() as db:
        assert db.query(User).filter_by(email="unverified@example.com").count() == 0


def test_state_mismatch_is_refused(client, monkeypatch):
    fake_google(monkeypatch, sub="g-csrf", email="csrf@example.com")
    start(client)
    r = finish(client, "forged-state")
    assert r.headers["location"] == "/?google=error"
    with SessionLocal() as db:
        assert db.query(User).filter_by(email="csrf@example.com").count() == 0


def test_user_cancelling_on_google_returns_to_app(client):
    r = client.get("/api/auth/google/callback?error=access_denied", follow_redirects=False)
    assert r.headers["location"] == "/?google=denied"


def test_google_only_account_cannot_use_password_login(client, monkeypatch):
    fake_google(monkeypatch, sub="g-only", email="only@example.com")
    token_from(finish(client, start(client)))
    for pw in ["", "anything123"]:
        r = client.post("/api/auth/login", json={"email": "only@example.com", "password": pw})
        assert r.status_code == 401
