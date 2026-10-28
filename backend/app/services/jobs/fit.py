"""Which new roles fit a user: matched on their target roles and skills, by words (no AI, so it costs nothing).

Generic job words ("engineer", "senior", "manager") don't count on their own: an "AI engineer" hears about AI
roles, not every engineering opening.
"""
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Match, Startup, User, now
from app.services.discovery import in_focus

GENERIC = {"engineer", "engineering", "developer", "senior", "junior", "lead", "staff", "principal", "manager", "head",
           "associate", "intern", "internship", "executive", "specialist", "of", "and", "the", "for", "role", "sr", "jr",
           "ii", "iii", "full", "time", "team", "member", "officer", "consultant", "analyst"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]+", (text or "").lower()) if len(w) >= 2} - GENERIC


def wanted_words(user: User) -> set[str]:
    p = user.profile
    if not p:
        return set()
    return _words(" ".join((p.target_roles or []) + (p.skills or [])))


def new_roles_for(db: Session, user: User, hours: int = 24, limit: int = 3) -> list[tuple[Job, Startup]]:
    """Roles first seen in the last `hours` at focus startups that fit the user (not at ones they skipped)."""
    wanted = wanted_words(user)
    if not wanted:
        return []
    skipped = set(db.scalars(select(Match.startup_id).where(Match.user_id == user.id, Match.status == "skipped")))
    rows = db.execute(select(Job, Startup).join(Startup, Job.startup_id == Startup.id)
                      .where(Job.closed_at.is_(None), Job.first_seen_at >= now() - timedelta(hours=hours))).all()
    hits = []
    for job, s in rows:
        if s.hidden or s.id in skipped or not in_focus(s.location, s.stage):
            continue
        overlap = len(wanted & _words(f"{job.title} {job.team}"))
        if overlap:
            hits.append((overlap, job, s))
    hits.sort(key=lambda h: h[0], reverse=True)
    return [(job, s) for _, job, s in hits[:limit]]


def message(roles: list[tuple[Job, Startup]]) -> str:
    lines = [f"• {job.title} at {s.name}{f' ({job.location})' if job.location else ''}: {job.url}" for job, s in roles]
    return ("🆕 New roles that fit you:\n" + "\n".join(lines) +
            "\n\nWant me to draft an email to the founder? Mentioning the role helps.")
