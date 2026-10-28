"""Landing page data: works signed out, stays inside the Bangalore focus, never leaks founders."""
import json
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Startup  # noqa: E402

ROWS = [
    dict(name="PubPay", domain="pubpay.in", one_liner="UPI payments for kirana stores", stage="seed", location="HSR Layout, Bengaluru",
         tags=["fintech"], source="startup_map", founders=[{"name": "Secret Founder", "title": "CEO"}],
         contacts=[{"email": "secret@pubpay.in", "confidence": "verified"}]),
    dict(name="PubMumbai", domain="pubmumbai.in", one_liner="UPI payments", stage="seed", location="Mumbai", tags=["fintech"]),
    dict(name="PubLate", domain="publate.in", one_liner="UPI payments", stage="series b", location="Bengaluru", tags=["fintech"]),
]


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        with SessionLocal() as db:
            for r in ROWS:
                if not db.query(Startup).filter_by(domain=r["domain"]).first():
                    db.add(Startup(**r))
            db.commit()
        yield c


@pytest.fixture(autouse=True)
def bangalore_focus(monkeypatch):
    monkeypatch.setattr(get_settings(), "focus_cities", "bengaluru,bangalore")
    monkeypatch.setattr(get_settings(), "focus_stages", "pre-seed,seed")
    main._preview_hits.clear()


def test_preview_finds_focus_startups_without_founders(client):
    rows = client.get("/api/public/preview", params={"q": "payments kirana"}).json()
    assert [r["name"] for r in rows] == ["PubPay"]  # Mumbai and Series B are out of focus
    r = rows[0]
    assert r["area"] == "HSR Layout" and r["has_founder"] is True and set(r["matched"]) == {"payments", "kirana"}
    raw = json.dumps(rows)
    assert "Secret Founder" not in raw and "secret@pubpay.in" not in raw


def test_preview_empty_query_and_no_match(client):
    assert client.get("/api/public/preview", params={"q": ""}).json() == []
    assert client.get("/api/public/preview", params={"q": "zzzunknownzzz"}).json() == []


def test_preview_is_rate_limited(client):
    codes = [client.get("/api/public/preview", params={"q": "payments"}).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_stats_and_fresh_count_only_focus(client):
    names = {r["name"] for r in client.get("/api/public/fresh").json()}
    assert "PubPay" in names and not names & {"PubMumbai", "PubLate"}
    assert client.get("/api/public/stats").json()["startups"] >= 1
    assert "Secret Founder" not in json.dumps(client.get("/api/public/fresh").json())


def test_map_startups_carry_their_credit_link(client):
    rows = client.get("/api/public/preview", params={"q": "payments kirana"}).json()
    assert rows and rows[0]["map_url"] is None  # PubPay's test row has no map page
    from app.services import matching
    with SessionLocal() as db:
        s = db.query(Startup).filter_by(domain="pubpay.in").one()
        s.source_url = "https://www.bangalorestartupmap.com/company/pubpay"
        db.commit()
        assert matching.public_card(s)["map_url"] == "https://www.bangalorestartupmap.com/company/pubpay"
