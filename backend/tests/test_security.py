"""Admin-only pool refresh, sign-in attempt limits, browser security headers."""
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    main._FAILS.clear()
    monkeypatch.setattr(main.discovery, "run_discovery", lambda db: 0)  # never hit the network


def signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_only_admins_can_refresh_the_pool(client, monkeypatch):
    h = signup(client, "user1@example.com")
    assert client.post("/api/admin/discover", headers=h).status_code == 403
    assert client.get("/api/me", headers=h).json()["is_admin"] is False
    monkeypatch.setattr(get_settings(), "admin_emails", "boss@example.com, User1@Example.com")
    # listed, but nobody has proved they own that address yet: not admin
    assert client.post("/api/admin/discover", headers=h).status_code == 403
    confirm(h)
    assert client.post("/api/admin/discover", headers=h).status_code == 200
    assert client.get("/api/me", headers=h).json()["is_admin"] is True


def confirm(headers):
    from app.auth import decode_token
    from app.db import SessionLocal
    from app.models import User
    from datetime import datetime, timezone
    with SessionLocal() as db:
        db.get(User, decode_token(headers["Authorization"].split()[1])).email_verified_at = datetime.now(timezone.utc)
        db.commit()


def test_five_wrong_passwords_lock_that_email(client):
    signup(client, "target@example.com")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "target@example.com", "password": "wrongpass1"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "target@example.com", "password": "password123"})
    assert r.status_code == 429 and "Too many sign-in attempts" in r.json()["detail"]
    # another account from the same machine is unaffected (the per-IP limit is higher)
    signup(client, "neighbour@example.com")
    assert client.post("/api/auth/login", json={"email": "neighbour@example.com", "password": "password123"}).status_code == 200


def test_correct_password_clears_the_count(client):
    signup(client, "forgetful@example.com")
    for _ in range(4):
        client.post("/api/auth/login", json={"email": "forgetful@example.com", "password": "nope-nope"})
    assert client.post("/api/auth/login", json={"email": "forgetful@example.com", "password": "password123"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "forgetful@example.com", "password": "nope-nope"}).status_code == 401


def test_one_ip_guessing_many_accounts_is_slowed(client):
    for i in range(20):
        client.post("/api/auth/login", json={"email": f"spray{i}@example.com", "password": "guess1234"})
    assert client.post("/api/auth/login", json={"email": "spray99@example.com", "password": "guess1234"}).status_code == 429


def test_security_headers(client, monkeypatch):
    for path in ("/", "/terms", "/api/public/stats"):
        h = client.get(path).headers
        assert "script-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "strict-transport-security" not in client.get("/").headers  # local http: no HSTS
    monkeypatch.setattr(get_settings(), "base_url", "https://founderreach.in")
    assert client.get("/").headers["strict-transport-security"].startswith("max-age=31536000")
