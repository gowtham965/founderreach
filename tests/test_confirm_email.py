"""Sign-up confirmation email: link, expiry, resend, the send lock, Google users, old accounts."""
import os
import tempfile

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("NEWS_FEEDS", "")

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.auth import create_token, decode_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Startup, User  # noqa: E402
from app.services import google_login, mailer  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_confirmation", lambda to, name, link: sent.append({"to": to, "link": link}))
    return sent


def signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123", "name": "Asha Rao"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def me(client, token):
    return client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()


def click(client, link):
    path = "/" + link.split("://", 1)[1].split("/", 1)[1]  # http://host/api/auth/verify?token=… → /api/…
    return client.get(path, follow_redirects=False)


def test_signup_sends_link_and_link_confirms(client, outbox):
    token = signup(client, "confirm1@example.com")
    assert me(client, token)["email_verified"] is False
    assert len(outbox) == 1 and outbox[0]["to"] == "confirm1@example.com"
    assert "/api/auth/verify?token=" in outbox[0]["link"]

    r = click(client, outbox[0]["link"])
    assert r.status_code in (302, 307) and r.headers["location"] == "/?verified=1"
    assert me(client, token)["email_verified"] is True
    assert click(client, outbox[0]["link"]).headers["location"] == "/?verified=1"  # clicking twice is fine


@pytest.mark.parametrize("bad", ["", "garbage", "SESSION"])
def test_bad_or_wrong_type_link_is_rejected(client, outbox, bad):
    token = signup(client, f"confirm-bad-{bad or 'empty'}@example.com".lower())
    link_token = token if bad == "SESSION" else bad  # a session token must not confirm an email
    r = client.get(f"/api/auth/verify?token={link_token}", follow_redirects=False)
    assert r.headers["location"] == "/?verified=expired"
    assert me(client, token)["email_verified"] is False


def test_expired_link_is_rejected(client, outbox):
    token = signup(client, "confirm-old@example.com")
    old = create_token(decode_token(token), purpose="verify_email", minutes=-1)
    assert client.get(f"/api/auth/verify?token={old}", follow_redirects=False).headers["location"] == "/?verified=expired"


def test_drafts_locked_until_confirmed(client, outbox):
    from app.models import Match
    from app.services import outreach

    token = signup(client, "confirm-lock@example.com")
    with SessionLocal() as db:
        user = db.get(User, decode_token(token))
        s = Startup(name="LockCo", domain="lockco.in")
        db.add(s)
        db.flush()
        m = Match(user_id=user.id, startup_id=s.id, score=80)
        db.add(m)
        db.commit()
        try:
            with pytest.raises(outreach.OutreachError, match="Confirm your email"):
                outreach.draft(db, user, m.id)
        finally:  # the test database is shared: keep LockCo out of other tests' startup pool
            db.rollback()
            db.delete(m)
            db.flush()
            db.delete(s)
            db.commit()


def test_resend_waits_a_minute(client, outbox):
    token = signup(client, "confirm-resend@example.com")
    h = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/auth/resend-verification", headers=h)
    assert r.status_code == 429 and len(outbox) == 1
    with SessionLocal() as db:
        db.get(User, decode_token(token)).verification_sent_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        db.commit()
    assert client.post("/api/auth/resend-verification", headers=h).status_code == 200
    assert len(outbox) == 2
    click(client, outbox[-1]["link"])
    assert client.post("/api/auth/resend-verification", headers=h).json() == {"ok": True, "already": True}
    assert len(outbox) == 2


def test_google_users_are_confirmed(client, outbox):
    with SessionLocal() as db:
        user, created = google_login.sign_in(db, {"sub": "g-confirm-1", "email": "gconfirm@example.com",
                                                  "email_verified": True, "name": "G"})
        assert created and user.email_verified_at
    token = signup(client, "link-later@example.com")
    with SessionLocal() as db:
        user, created = google_login.sign_in(db, {"sub": "g-confirm-2", "email": "link-later@example.com",
                                                  "email_verified": True})
        assert not created and user.email_verified_at  # Google proved they own the address
    assert me(client, token)["email_verified"] is True


def test_existing_accounts_count_as_confirmed(monkeypatch, tmp_path):
    """The column is added to an old database: accounts already there must not be locked out."""
    from sqlalchemy import create_engine, text

    from app import db as db_mod

    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255), name VARCHAR(255), "
                          "password_hash VARCHAR(255), created_at DATETIME)"))
        conn.execute(text("INSERT INTO users (email, name, password_hash) VALUES ('old@example.com', '', 'x')"))
    monkeypatch.setattr(db_mod, "engine", old)
    db_mod._add_missing_columns()
    with old.connect() as conn:
        row = conn.execute(text("SELECT email_verified_at, verification_sent_at FROM users")).one()
    assert row[0] is not None and row[1] is None


def test_mailer_without_smtp_logs_the_link(monkeypatch, caplog):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "smtp_user", "")
    monkeypatch.setattr(mailer.smtplib, "SMTP", lambda *a, **k: pytest.fail("no SMTP without settings"))
    mailer.send_confirmation("a@example.com", "Asha", "http://x/api/auth/verify?token=t")
    assert "http://x/api/auth/verify?token=t" in caplog.text


def test_mailer_sends_over_smtp(monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "smtp_user", "me@gmail.com")
    monkeypatch.setattr(s, "smtp_password", "app-password")
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout): calls["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context): calls["tls"] = True
        def login(self, u, p): calls["login"] = (u, p)
        def send_message(self, msg): calls["msg"] = msg

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    mailer.send_confirmation("asha@example.com", "Asha Rao", "http://localhost:8000/api/auth/verify?token=abc")
    msg = calls["msg"]
    assert calls["host"] == ("smtp.gmail.com", 587) and calls["tls"] and calls["login"] == ("me@gmail.com", "app-password")
    assert msg["To"] == "asha@example.com" and msg["From"] == "FounderReach <me@gmail.com>"
    assert "http://localhost:8000/api/auth/verify?token=abc" in msg.get_body(("plain",)).get_content()
    assert "Hi Asha," in msg.get_body(("plain",)).get_content()
