"""Ops agent: daily numbers, rule-based alerts, delivered only to admins."""
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ChatMessage, JobRun, LLMUsage, Payment, User, now  # noqa: E402
from app.services import ops  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean(client):  # the app creates the tables on startup
    main._FAILS.clear()
    main._SIGNUPS.clear()
    with SessionLocal() as db:
        db.query(JobRun).delete()
        db.query(LLMUsage).delete()
        db.commit()
    yield


def runs(*details, ok=True, ago_hours=(50, 26, 2)):
    with SessionLocal() as db:
        for d, h in zip(details, ago_hours):
            db.add(JobRun(name="discovery", ok=ok, detail=d, started_at=now() - timedelta(hours=h)))
        db.commit()


def report_alerts():
    with SessionLocal() as db:
        return ops.alerts(ops.collect(db))


def test_quiet_and_healthy():
    runs({"fetch_news": 2, "fetch_startup_map": 1}, {"fetch_news": 1, "fetch_startup_map": 0}, {"fetch_news": 3, "fetch_startup_map": 0})
    with SessionLocal() as db:
        db.add(JobRun(name="backup", ok=True, detail={"file": "x"}, started_at=now() - timedelta(hours=5)))
        db.commit()
    assert report_alerts() == []


def test_missing_backup_is_an_alert():
    runs({"fetch_news": 1})
    assert any("No successful database backup" in a for a in report_alerts())


def test_source_gone_quiet_and_failed_job():
    runs({"fetch_news": 0, "fetch_yc_hiring": 1}, {"fetch_news": 0, "fetch_yc_hiring": 0},
         {"fetch_news": 0, "fetch_yc_hiring": "failed: timeout"})
    with SessionLocal() as db:
        db.add(JobRun(name="followups", ok=False, detail={"error": "boom"}, started_at=now() - timedelta(hours=1)))
        db.commit()
    text = " ".join(report_alerts())
    assert "'fetch_news' added 0 startups in the last 3" in text and "'followups' failed" in text
    assert "fetch_yc_hiring' added 0" not in text  # a failure isn't "quiet"; it's reported as a failure


def test_scheduler_stopped():
    assert any("hasn't run in over a day" in a for a in report_alerts())


def test_stuck_payment_and_spend_spike():
    runs({"fetch_news": 1})
    with SessionLocal() as db:
        db.add(Payment(user_id=None, email="x@gmail.com", merchant_order_id="FR1-0000000000aa", plan="week",
                       amount_paise=29900, state="PENDING", created_at=now() - timedelta(hours=3)))
        db.add(LLMUsage(task="chat", model="gpt-6.1-sol", input_tokens=4_000_000, output_tokens=0))  # ₹704 today
        db.commit()
    text = " ".join(report_alerts())
    assert "FR1-0000000000aa" in text and "over twice the 7-day average" in text
    with SessionLocal() as db:
        db.query(Payment).filter_by(merchant_order_id="FR1-0000000000aa").delete()
        db.commit()


def test_report_has_the_numbers():
    with SessionLocal() as db:
        text = ops.build(db)
    assert text.startswith("📊 Ops report") and "Users:" in text and "AI spend: ₹" in text


def test_only_admins_get_it(client, monkeypatch):
    r = client.post("/api/auth/signup", json={"email": "boss@gmail.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    r2 = client.post("/api/auth/signup", json={"email": "someone@gmail.com", "password": "password123"})
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    with SessionLocal() as db:
        for e in ("boss@gmail.com", "someone@gmail.com"):
            db.query(User).filter_by(email=e).one().email_verified_at = now()
        db.commit()
    monkeypatch.setattr(get_settings(), "admin_emails", "boss@gmail.com")
    assert client.post("/api/admin/ops-report", headers=h2).status_code == 403
    assert client.post("/api/admin/ops-report", headers=h).json()["report"].startswith("📊")
    with SessionLocal() as db:
        assert ops.deliver(db) == 1
        boss = db.query(User).filter_by(email="boss@gmail.com").one()
        other = db.query(User).filter_by(email="someone@gmail.com").one()
        assert db.query(ChatMessage).filter(ChatMessage.user_id == boss.id, ChatMessage.content.like("📊%")).count() == 2
        assert db.query(ChatMessage).filter(ChatMessage.user_id == other.id, ChatMessage.content.like("📊%")).count() == 0
