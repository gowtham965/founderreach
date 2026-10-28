"""Keep each startup's open roles current: scan its site and job boards, save roles with their history, run nightly.

A role is closed only after two checks in a row where its board answered and the role was gone; a board that
didn't answer changes nothing. Startup.jobs caches the open roles for cards, drafts and the chat.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Startup, aware, now
from app.services.discovery import INDIA_PLACES, in_focus
from app.services.jobs import boards, detect, jsonld

log = logging.getLogger(__name__)
GUESS_EVERY = timedelta(days=7)   # re-guess a board for startups with none linked, at most weekly
OTHERS_EVERY = timedelta(days=7)  # startups outside the focus are checked weekly
_SUFFIX = re.compile(r"(private|pvt|limited|ltd|inc|llp|technologies|technology|tech|labs|software|solutions|india|hq)$")


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    for _ in range(3):
        n = _SUFFIX.sub("", n)
    return n


def same_company(board_name: str | None, startup_name: str) -> bool:
    """A guessed board is trusted only if it names the startup ("Razorpay Software Private Limited" ~ "Razorpay")."""
    a, b = _norm(board_name or ""), _norm(startup_name)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def plausible(res: dict, startup_name: str) -> bool:
    """A guessed board (not linked from their site) must name the startup AND list a role in India or remote:
    short names ("Infinity", "xPay") are shared by companies elsewhere."""
    if not same_company(res.get("name"), startup_name):
        return False
    return any(j["remote"] or any(p in j["location"].lower() for p in INDIA_PLACES) for j in res.get("jobs", []))


def scan(s: Startup) -> tuple[list[dict], set[str], dict]:
    """(roles found, sources that answered, updated job_boards)."""
    state = dict(s.job_boards or {})
    known = dict(state.get("boards") or {})
    pages = detect.careers_pages(s.domain)
    for _, html in pages:
        for board, slug in detect.boards_in(html).items():
            known.setdefault(board, slug)
    found, answered = [], set()
    if pages:
        answered.add("careers_page")
        for url, html in pages:
            found += [{**j, "board": "careers_page"} for j in jsonld.job_postings(html, url)]
    for board, slug in known.items():
        res = boards.read(board, slug)
        if res is not None:
            answered.add(board)
            found += [{**j, "board": board} for j in res["jobs"] if j["title"]]
    guessed = state.get("guessed_at")
    if not known and (not guessed or aware(datetime.fromisoformat(guessed)) < now() - GUESS_EVERY):
        slugs = list(dict.fromkeys([s.domain.split(".")[0], re.sub(r"[^a-z0-9]", "", s.name.lower())]))
        for board in boards.NAMED:
            hit = next(((slug, res) for slug in slugs if (res := boards.read(board, slug)) is not None
                        and plausible(res, s.name)), None)
            if hit:
                known[board] = hit[0]
                answered.add(board)
                found += [{**j, "board": board} for j in hit[1]["jobs"] if j["title"]]
                break
        state["guessed_at"] = now().isoformat()
    state["boards"] = known
    return found, answered, state


def _dedupe(found: list[dict]) -> list[dict]:
    """One row per link; a careers-page copy of a role its job board already lists is dropped."""
    by_url, board_titles = {}, {j["title"].lower() for j in found if j["board"] != "careers_page"}
    for j in found:
        if j["url"] and j["url"] not in by_url and not (j["board"] == "careers_page" and j["title"].lower() in board_titles):
            by_url[j["url"]] = j
    return list(by_url.values())


def save(db: Session, s: Startup, found: list[dict], answered: set[str]) -> dict:
    """Upsert roles, count misses, close the gone ones, refresh Startup.jobs. Returns {"new", "closed", "open"}."""
    at = now()
    rows = {j.url: j for j in db.scalars(select(Job).where(Job.startup_id == s.id))}
    new = closed = 0
    for j in _dedupe(found):
        row = rows.pop(j["url"], None)
        if row is None:
            posted = datetime.fromisoformat(j["posted"]).replace(tzinfo=timezone.utc) if j.get("posted") and len(j["posted"]) == 10 else None
            db.add(Job(startup_id=s.id, title=j["title"], team=j["team"], location=j["location"], remote=j["remote"],
                       url=j["url"], board=j["board"], posted_at=posted, first_seen_at=at, last_seen_at=at))
            new += 1
        else:
            row.title, row.team, row.location, row.remote = j["title"], j["team"], j["location"], j["remote"]
            row.last_seen_at, row.missed, row.closed_at = at, 0, None
    for row in rows.values():  # not seen this time
        if row.closed_at is None and row.board in answered:
            row.missed += 1
            if row.missed >= 2:
                row.closed_at = at
                closed += 1
    db.flush()
    open_rows = db.scalars(select(Job).where(Job.startup_id == s.id, Job.closed_at.is_(None))).all()
    open_rows = sorted(open_rows, key=lambda r: aware(r.posted_at or r.first_seen_at), reverse=True)
    s.jobs = [{"title": r.title, "team": r.team, "location": r.location, "remote": r.remote, "url": r.url, "board": r.board,
               "posted": (r.posted_at or r.first_seen_at).date().isoformat()} for r in open_rows[:12]]
    s.jobs_checked_at = at
    db.commit()
    return {"new": new, "closed": closed, "open": len(open_rows)}


def refresh_startup(db: Session, s: Startup) -> dict:
    if not s.domain:
        return {"new": 0, "closed": 0, "open": 0}
    found, answered, state = scan(s)
    s.job_boards = state
    return save(db, s, found, answered)


def due(db: Session, weekly_cap: int = 150) -> list[int]:
    """Who to check tonight: every focus startup, then the oldest-checked others (at most weekly, capped)."""
    everyone = [s for s in db.scalars(select(Startup).where(Startup.domain.is_not(None))) if not s.hidden]
    focus = [s.id for s in everyone if in_focus(s.location, s.stage)]
    cutoff = now() - OTHERS_EVERY
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    others = sorted((s for s in everyone if not in_focus(s.location, s.stage)
                     and (not s.jobs_checked_at or aware(s.jobs_checked_at) < cutoff)),
                    key=lambda s: aware(s.jobs_checked_at) or old)
    return focus + [s.id for s in others[:weekly_cap]]


def run(db: Session, budget_minutes: int = 40) -> dict:
    """The nightly refresh, stopping when the time budget runs out (the rest are first in line next night)."""
    started, stats = time.monotonic(), {"checked": 0, "hiring": 0, "new_roles": 0, "closed_roles": 0, "failed": 0, "left": 0}
    ids = due(db)
    for i, sid in enumerate(ids):
        if time.monotonic() - started > budget_minutes * 60:
            stats["left"] = len(ids) - i
            break
        s = db.get(Startup, sid)
        try:
            r = refresh_startup(db, s)
        except Exception:
            log.exception("job refresh failed for %s", s.name if s else sid)
            db.rollback()
            stats["failed"] += 1
            continue
        stats["checked"] += 1
        stats["hiring"] += r["open"] > 0
        stats["new_roles"] += r["new"]
        stats["closed_roles"] += r["closed"]
    return stats
