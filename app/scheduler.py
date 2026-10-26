"""Background jobs (in-process APScheduler).

  daily  {digest_hour-1}:00 IST  discovery: refresh the startup pool
  daily  {digest_hour}:30 IST    matching: new picks for every ready user + a chat notification
  hourly                         follow-up reminders (writes the draft; the user sends it)
For multiple server instances, move these to a single worker process or a cron service.
"""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import User
from app.services import discovery, matching, outreach

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def job_discovery():
    with SessionLocal() as db:
        log.info("discovery added %d startups", discovery.run_discovery(db))


def wants_daily_matches(user: User, now: datetime | None = None) -> bool:
    """Confirmed, has a profile, and used the app recently (web or WhatsApp). Matching costs AI
    credits, and nobody reads the 40th morning message in a row."""
    if not user.email_verified_at or not user.profile or not user.profile.is_ready():
        return False
    now = now or datetime.now(timezone.utc)
    seen = [t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
            for t in (user.last_seen_at, user.whatsapp_last_inbound_at, user.created_at) if t]
    return bool(seen) and now - max(seen) <= timedelta(days=get_settings().match_active_days)


def job_daily_matches():
    with SessionLocal() as db:
        for user in db.scalars(select(User)).all():
            try:
                if not wants_daily_matches(user):
                    continue
                new = matching.find_matches(db, user)
                if not new:
                    continue
                lines = [f"• #{m.id} {m.startup.name}: {m.startup.one_liner[:80]} ({m.reason})" for m in new[:5]]
                outreach.notify(db, user.id, f"gm ☀️ {len(new)} new startups fit you today:\n" + "\n".join(lines)
                                + "\n\nWant me to draft an email to any of them?")
            except Exception:
                log.exception("daily matches failed for user %s", user.id)
                db.rollback()


def job_followups():
    with SessionLocal() as db:
        outreach.prepare_due_followups(db)


def start():
    global _scheduler
    s = get_settings()
    if not s.scheduler_enabled or _scheduler:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    _scheduler.add_job(job_discovery, "cron", hour=max(s.digest_hour_ist - 1, 0), minute=0, id="discovery")
    _scheduler.add_job(job_daily_matches, "cron", hour=s.digest_hour_ist, minute=30, id="matches")
    _scheduler.add_job(job_followups, "interval", hours=1, id="followups")
    _scheduler.start()
    log.info("scheduler started")


def stop():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
