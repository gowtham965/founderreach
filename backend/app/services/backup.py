"""Nightly database backups (SQLite only; a managed Postgres has its own).

SQLite's online backup API copies a consistent snapshot while the app keeps serving. Each copy is checked
(PRAGMA integrity_check), gzipped, and kept next to the database in backups/ for BACKUP_KEEP_DAYS. Those copies
live on the same Render disk, so the admin "Download backup" button is how a copy leaves the server.

Restore (Render Shell, with the service suspended or right after a deploy starts):
    gunzip -c /var/data/backups/founderreach-YYYY-MM-DD-HHMM.db.gz > /var/data/founderreach.db
"""
import gzip
import logging
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)
PREFIX = "founderreach-"


def db_path() -> Path | None:
    """The SQLite file behind DATABASE_URL, or None for other databases."""
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    return Path(url[len("sqlite:///"):]).resolve()


def backup_dir() -> Path | None:
    src = db_path()
    return src.parent / "backups" if src else None


def run(at: datetime | None = None) -> dict:
    """Make one backup; returns {"file", "bytes", "kept"}. Raises if the copy can't be made or fails its check."""
    src = db_path()
    if src is None:
        raise RuntimeError("Backups here are for SQLite; use your database provider's backups")
    if not src.exists():
        raise RuntimeError(f"Database file not found: {src}")
    at = at or datetime.now(timezone.utc)
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{at:%Y-%m-%d-%H%M}.db.gz"
    tmp = out_dir / f".{name}.tmp"
    try:
        with sqlite3.connect(src) as source, sqlite3.connect(tmp) as dest:
            source.backup(dest)  # consistent snapshot, safe while the app writes
            ok = dest.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"backup failed its integrity check: {ok}")
        with open(tmp, "rb") as f_in, gzip.open(out_dir / name, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    finally:
        tmp.unlink(missing_ok=True)
    kept = prune(at)
    size = (out_dir / name).stat().st_size
    log.info("backup %s (%d bytes), %d kept", name, size, kept)
    return {"file": name, "bytes": size, "kept": kept}


def prune(at: datetime | None = None) -> int:
    """Delete backups older than BACKUP_KEEP_DAYS (by the date in the name); always keep the newest."""
    out_dir = backup_dir()
    files = list_backups()
    cutoff = (at or datetime.now(timezone.utc)) - timedelta(days=get_settings().backup_keep_days)
    for f in files[1:]:
        try:
            made = datetime.strptime(f.name[len(PREFIX):-len(".db.gz")], "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if made < cutoff:
            f.unlink(missing_ok=True)
    return len(list_backups()) if out_dir else 0


def list_backups() -> list[Path]:
    """Newest first."""
    out_dir = backup_dir()
    if not out_dir or not out_dir.exists():
        return []
    return sorted(out_dir.glob(f"{PREFIX}*.db.gz"), reverse=True)


def latest() -> Path | None:
    files = list_backups()
    return files[0] if files else None
