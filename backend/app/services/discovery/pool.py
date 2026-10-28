"""The startup pool: every source in SOURCES, saved with upsert (hand edits and credits kept)."""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Startup
from app.services.discovery.focus import in_focus, keep_in_database
from app.services.discovery.news import fetch_news
from app.services.discovery.startup_map import fetch_startup_map
from app.services.discovery.yc import fetch_yc_hiring

log = logging.getLogger(__name__)

SOURCES = [fetch_yc_hiring, fetch_news, fetch_startup_map]


def upsert_startups(db: Session, rows: list[dict]) -> int:
    added = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        domain = row.get("domain")
        existing = None
        if domain:
            existing = db.scalar(select(Startup).where(Startup.domain == domain))
        if existing is None:
            existing = db.scalar(select(Startup).where(Startup.name == name))
        fields = {k: row.get(k) for k in ("one_liner", "stage", "location", "tags", "signal", "source", "source_url") if row.get(k)}
        if existing is not None and existing.source == "startup_map" and fields.get("source") != "startup_map":
            # Keep the Bangalore Startup Map credit (a condition of using it); other facts still update.
            fields.pop("source", None)
            fields.pop("source_url", None)
        if existing:
            for k in existing.edited or []:  # an admin fixed this field by hand: discovery leaves it alone
                fields.pop(k, None)
            for k, v in fields.items():
                setattr(existing, k, v)
            if row.get("founders") and not existing.founders:
                existing.founders = row["founders"]
            existing.discovered_at = datetime.now(timezone.utc)
        else:
            db.add(Startup(name=name, domain=domain, founders=row.get("founders") or [], **fields))
            added += 1
        db.flush()
    db.commit()
    return added


def run_discovery(db: Session, stats: dict | None = None) -> int:
    """Refresh the pool from every source. `stats`, if given, gets {source: added, or "failed: ..."}."""
    total = 0
    for source in SOURCES:
        try:
            rows = source()
            kept = [r for r in rows if keep_in_database(r)]
            added = upsert_startups(db, kept)
            total += added
            if stats is not None:
                stats[source.__name__] = added
            log.info("%s: %d rows, %d kept (India), %d in focus", source.__name__, len(rows), len(kept),
                     sum(in_focus(r.get("location"), r.get("stage")) for r in kept))
        except Exception as exc:
            if stats is not None:
                stats[source.__name__] = f"failed: {str(exc)[:120]}"
            log.warning("discovery source %s failed: %s", source.__name__, exc)
    return total
