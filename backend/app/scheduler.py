"""Background jobs (in-process APScheduler).

  daily  {digest_hour-1}:00 IST  discovery: refresh the startup pool
  daily  {digest_hour}:30 IST    matching: new picks for every ready user + a chat notification
  hourly                         follow-up reminders (writes the draft; the user sends it)
  daily  {ops_report_hour}:00 IST    ops report to the admins (app/services/ops.py)
  daily  {backup_hour}:00 IST        database backup, last 7 kept (app/services/backup.py)
  daily  {jobs_hour}:00 IST          open roles: focus startups nightly, the rest weekly (app/services/jobs/)
For multiple server instances, move these to a single worker process or a cron service.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.llm import usage
from app.models import JobRun, User
from app.agents import ops
from app.services import alerts, backup, billing, discovery, jobs, matching, outreach

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def record(name: str, ok: bool, detail: dict | None = None) -> None:
    """Log a job run for the ops report (and alert the admin's phone when it failed). Never breaks the job."""
    if not ok:
        alerts.problem(f"Background job '{name}' failed: {str(detail)[:200]}")
    try:
        with SessionLocal() as db:
            db.add(JobRun(name=name, ok=ok, detail=detail))
            db.commit()
    except Exception:
        log.exception("could not record job run %s", name)


def job_discovery():
    stats: dict = {}
    try:
        with SessionLocal() as db:
            log.info("discovery added %d startups", discovery.run_discovery(db, stats))
        record("discovery", not any(str(v).startswith("failed") for v in stats.values()), stats)
    except Exception as exc:
        record("discovery", False, {**stats, "error": str(exc)[:200]})
        raise


def wants_daily_matches(user: User, now: datetime | None = None) -> bool:
    """Confirmed, has a profile, and used the app recently (web or WhatsApp). Matching costs AI
    credits, and nobody reads the 40th morning message in a row."""
    if not user.email_verified_at or not user.profile or not user.profile.is_ready():
        return False
    if not billing.has_access(user, now):  # trial over, no pass: no new matches (or AI cost)
        return False
    now = now or datetime.now(timezone.utc)
    seen = [t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
            for t in (user.last_seen_at, user.whatsapp_last_inbound_at, user.created_at) if t]
    return bool(seen) and now - max(seen) <= timedelta(days=get_settings().match_active_days)


def job_daily_matches():
    sent = failed = roles_sent = 0
    with SessionLocal() as db:
        for user in db.scalars(select(User)).all():
            try:
                if not wants_daily_matches(user):
                    continue
                billing.require_access(db, user)  # morning matches count as use: starts the trial clock
                with usage.for_user(user):
                    new = matching.find_matches(db, user)
                roles = jobs.fit.new_roles_for(db, user)  # roles first seen since yesterday (the 2am refresh)
                if roles:
                    outreach.notify(db, user.id, jobs.fit.message(roles))
                    roles_sent += 1
                if not new:
                    continue
                lines = [f"• #{m.id} {m.startup.name}: {m.startup.one_liner[:80]} ({m.reason})" for m in new[:5]]
                outreach.notify(db, user.id, f"gm ☀️ {len(new)} new startups fit you today:\n" + "\n".join(lines)
                                + "\n\nWant me to draft an email to any of them?")
                sent += 1
            except (billing.PaywallError, usage.UserBudgetExceeded):
                db.rollback()
            except Exception:
                log.exception("daily matches failed for user %s", user.id)
                failed += 1
                db.rollback()
    record("daily_matches", failed == 0, {"users_notified": sent, "role_alerts": roles_sent, "failed": failed})


def job_followups():
    try:
        with SessionLocal() as db:
            n = outreach.prepare_due_followups(db)
        record("followups", True, {"drafted": n})
    except Exception as exc:
        record("followups", False, {"error": str(exc)[:200]})
        raise


_jobs_running = threading.Lock()


def job_jobs():
    """Open roles: every focus startup nightly, the rest weekly (app/services/jobs/). One run at a time."""
    if not _jobs_running.acquire(blocking=False):
        log.info("open-roles refresh already running; skipped")
        return
    try:
        with SessionLocal() as db:
            stats = jobs.run(db)
        record("jobs", stats["failed"] < max(3, stats["checked"] // 10), stats)
    except Exception as exc:
        log.exception("job refresh failed")
        record("jobs", False, {"error": str(exc)[:200]})
    finally:
        _jobs_running.release()


def job_backup():
    if backup.db_path() is None:  # not SQLite: the database provider backs it up
        return
    try:
        record("backup", True, backup.run())
    except Exception as exc:
        log.exception("backup failed")
        record("backup", False, {"error": str(exc)[:200]})


def job_ops_report():
    try:
        with SessionLocal() as db:
            ops.deliver(db)
    except Exception:
        log.exception("ops report failed")


def start():
    global _scheduler
    s = get_settings()
    if not s.scheduler_enabled or _scheduler:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    _scheduler.add_job(job_discovery, "cron", hour=max(s.digest_hour_ist - 1, 0), minute=0, id="discovery")
    _scheduler.add_job(job_daily_matches, "cron", hour=s.digest_hour_ist, minute=30, id="matches")
    _scheduler.add_job(job_followups, "interval", hours=1, id="followups")
    _scheduler.add_job(job_ops_report, "cron", hour=s.ops_report_hour_ist, minute=0, id="ops_report")
    _scheduler.add_job(job_backup, "cron", hour=s.backup_hour_ist, minute=0, id="backup")
    _scheduler.add_job(job_jobs, "cron", hour=s.jobs_hour_ist, minute=0, id="jobs")
    _scheduler.start()
    log.info("scheduler started")


def stop():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
