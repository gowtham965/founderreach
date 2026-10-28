"""Nightly backups: a checked, compressed copy that restores; old ones pruned; admins can download."""
import gzip
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main, scheduler  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import JobRun, User, now  # noqa: E402
from app.services import backup  # noqa: E402


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "live.db"
    with sqlite3.connect(path) as c:
        c.execute("create table users (id integer primary key, email text)")
        c.executemany("insert into users (email) values (?)", [("a@x.com",), ("b@x.com",)])
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite:///{path}")
    return path


def restore(gz_path, tmp_path):
    out = tmp_path / "restored.db"
    out.write_bytes(gzip.decompress(gz_path.read_bytes()))
    with sqlite3.connect(out) as c:
        return [r[0] for r in c.execute("select email from users order by id")]


def test_backup_is_a_working_copy(db_file, tmp_path):
    made = backup.run()
    f = backup.backup_dir() / made["file"]
    assert f.exists() and f.parent == db_file.parent / "backups" and made["bytes"] > 0
    assert restore(f, tmp_path) == ["a@x.com", "b@x.com"]
    assert not list(f.parent.glob(".*.tmp"))  # no half-written leftovers


def test_old_backups_are_pruned(db_file, monkeypatch):
    monkeypatch.setattr(get_settings(), "backup_keep_days", 7)
    start = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
    for d in range(12):
        backup.run(start + timedelta(days=d))
    names = [p.name for p in backup.list_backups()]
    assert names[0] == "founderreach-2026-10-12-0300.db.gz" and names[-1] == "founderreach-2026-10-05-0300.db.gz"
    assert len(names) == 8  # today plus the 7 days before


def test_not_sqlite_is_skipped(monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", "postgresql://u:p@host/db")
    assert backup.db_path() is None
    scheduler.job_backup()  # no error, nothing to do


def test_nightly_job_records_runs(db_file):
    from app.db import init_db
    init_db()
    with SessionLocal() as db:
        db.query(JobRun).filter_by(name="backup").delete()
        db.commit()
    scheduler.job_backup()
    with SessionLocal() as db:
        run = db.query(JobRun).filter_by(name="backup").one()
    assert run.ok and run.detail["file"].startswith("founderreach-")


def test_only_admins_can_download(db_file, monkeypatch, tmp_path):
    with TestClient(main.app) as client:
        main._FAILS.clear()
        main._SIGNUPS.clear()
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite:///{db_file}")  # startup re-reads nothing; keep ours
        h = {}
        for e in ("owner@gmail.com", "user@gmail.com"):
            r = client.post("/api/auth/signup", json={"email": e, "password": "password123"})
            h[e] = {"Authorization": f"Bearer {r.json()['token']}"}
        with SessionLocal() as db:
            for e in h:
                db.query(User).filter_by(email=e).one().email_verified_at = now()
            db.commit()
        monkeypatch.setattr(get_settings(), "admin_emails", "owner@gmail.com")
        assert client.get("/api/admin/backup", headers=h["user@gmail.com"]).status_code == 403
        r = client.get("/api/admin/backup", headers=h["owner@gmail.com"])
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
        assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
        (tmp_path / "dl.db.gz").write_bytes(r.content)
        assert restore(tmp_path / "dl.db.gz", tmp_path) == ["a@x.com", "b@x.com"]
