"""Policy pages payment gateways check for, and account deletion they promise."""
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


@pytest.mark.parametrize("path,heading", [("/terms", "Terms &amp; Conditions"), ("/privacy", "Privacy Policy"),
                                          ("/refunds", "Refund &amp; Cancellation Policy"), ("/contact", "Contact Us")])
def test_policy_pages(client, path, heading):
    r = client.get(path)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert f"<h1>{heading}</h1>" in r.text and "Gowtham R Gowda" in r.text


def test_details_come_from_settings_and_are_escaped(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "support_email", "help@founderreach.in")
    monkeypatch.setattr(s, "business_address", "12 <b>MG</b> Road, Bengaluru 560001")
    page = client.get("/contact").text
    assert 'href="mailto:help@founderreach.in"' in page
    assert "12 &lt;b&gt;MG&lt;/b&gt; Road" in page and "<b>MG</b>" not in page
    monkeypatch.setattr(s, "support_email", "")
    monkeypatch.setattr(s, "business_address", "")
    page = client.get("/contact").text
    assert "coming soon" in page and "Address" not in page


def test_privacy_matches_how_the_app_works(client):
    text = client.get("/privacy").text
    assert "never get access to your Gmail" in text and "Anthropic" in text and "bangalorestartupmap.com" in text
    assert "never send email on your behalf" in client.get("/terms").text


def test_account_deletion_removes_everything(client):
    r = client.post("/api/auth/signup", json={"email": "delete-me@example.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.delete("/api/me", headers=h).json() == {"ok": True}
    assert client.get("/api/me", headers=h).status_code == 401
    assert client.post("/api/auth/login", json={"email": "delete-me@example.com", "password": "password123"}).status_code == 401
