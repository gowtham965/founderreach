"""Pick the startups that fit a user.

Two stages: a cheap keyword prefilter over the whole pool, then the LLM scores the
top candidates against the profile and explains each fit in one line.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import get_llm
from app.models import Match, Startup, User
from app.services.discovery import in_focus
from app.services.outreach import compose_links
from app.services.profile import profile_brief

SCORE_SYSTEM = """You are a sharp startup recruiter. Score how well each startup fits this
candidate (0-100) given their skills, wins and stated preferences. Respect hard
preferences (stage, remote, location, sectors to avoid) strictly: a violation caps the
score at 30. The reason must be one specific sentence connecting the candidate's
experience to what the startup does. No generic praise. If the startup has an open role that fits the
candidate (listed under "hiring"), score higher and name that role in the reason."""

SCORE_PROMPT = """Candidate:
{profile}

Startups:
{startups}

Return a JSON array: [{{"id": int, "score": int, "reason": str}}]"""


# Two-letter words that are just English; everything else of 2+ characters is kept, so short
# skills ("ai", "ml", "go", "ui", "ux", "qa", "c#", "c++") count.
_STOP = {"an", "as", "at", "be", "by", "do", "for", "if", "in", "is", "it", "me", "my", "no", "of", "on", "or",
         "so", "to", "up", "us", "we", "and", "the", "with", "who", "you", "are", "our", "can", "has", "its", "not"}


def _tokens(text: str) -> set[str]:
    words = (w.rstrip(".") for w in re.findall(r"[a-z0-9][a-z0-9+#.]*", text.lower()))
    return {w for w in words if len(w) >= 2 and w not in _STOP}


def _prefilter(user: User, candidates: list[Startup], keep: int = 40) -> list[Startup]:
    p = user.profile
    wanted = _tokens(" ".join((p.skills or []) + (p.target_roles or []) + [p.summary or "", json.dumps(p.preferences or {})]))

    def score(s: Startup) -> int:
        about = len(wanted & _tokens(" ".join([s.one_liner, " ".join(s.tags or []), s.signal, s.stage, s.location])))
        roles = len(wanted & _tokens(" ".join(j.get("title", "") for j in s.jobs or [])))
        return about + 2 * roles  # hiring for what they do counts double

    return sorted(candidates, key=score, reverse=True)[:keep]


def find_matches(db: Session, user: User, limit: int | None = None) -> list[Match]:
    limit = limit or get_settings().daily_matches
    if not user.profile or not user.profile.is_ready():
        return []
    seen = select(Match.startup_id).where(Match.user_id == user.id)
    since = datetime.now(timezone.utc) - timedelta(days=45)
    pool = [s for s in db.scalars(select(Startup).where(Startup.id.not_in(seen), Startup.discovered_at >= since))
            if in_focus(s.location, s.stage) and not s.hidden]
    if not pool:
        return []
    shortlist = _prefilter(user, pool)
    llm = get_llm()
    scored: list[dict] = []
    for i in range(0, len(shortlist), 20):
        chunk = shortlist[i : i + 20]
        listing = "\n".join(
            json.dumps({"id": s.id, "name": s.name, "what": s.one_liner, "stage": s.stage, "location": s.location,
                        "tags": (s.tags or [])[:6], "signal": s.signal,
                        "hiring": [j.get("title", "") for j in (s.jobs or [])[:5]]})
            for s in chunk
        )
        rows = llm.complete_json(SCORE_SYSTEM, SCORE_PROMPT.format(profile=profile_brief(user.profile), startups=listing),
                                 task="score_matches")
        valid_ids = {s.id for s in chunk}
        scored += [r for r in rows or [] if r.get("id") in valid_ids]
    scored.sort(key=lambda r: r.get("score", 0), reverse=True)
    created = []
    for r in scored[:limit]:
        if r.get("score", 0) < 55:
            break
        m = Match(user_id=user.id, startup_id=r["id"], score=r["score"], reason=r.get("reason", ""))
        db.add(m)
        created.append(m)
    db.commit()
    return created


# ---------- public landing page: no account, no LLM, no founder details ----------
def _focus_pool(db: Session) -> list[Startup]:
    since = datetime.now(timezone.utc) - timedelta(days=45)
    return [s for s in db.scalars(select(Startup).where(Startup.discovered_at >= since).order_by(Startup.discovered_at.desc()))
            if in_focus(s.location, s.stage) and not s.hidden]


def _area(location: str) -> str:
    """'HSR Layout, Bengaluru' → 'HSR Layout'; plain 'Bengaluru' stays."""
    first = (location or "").split(",")[0].strip()
    return first or "Bengaluru"


def public_card(s: Startup, matched: list[str] | None = None) -> dict:
    card = {"name": s.name, "area": _area(s.location), "stage": s.stage, "what": s.one_liner,
            "signal": s.signal, "news_url": s.source_url if s.source == "news" else None,
            "map_url": (s.source_url or None) if s.source == "startup_map" else None,  # credited, as agreed with the directory
            "has_founder": bool(s.founders or s.contacts)}
    if matched is not None:
        card["matched"] = matched
    return card


def public_stats(db: Session) -> dict:
    return {"startups": len(_focus_pool(db))}


def public_fresh(db: Session, limit: int = 12) -> list[dict]:
    return [public_card(s) for s in _focus_pool(db)[:limit]]


def public_preview(db: Session, query: str, limit: int = 3) -> list[dict]:
    """Keyword overlap only: free and instant. Founder names and emails stay on the server."""
    wanted = _tokens(query)
    if not wanted:
        return []
    scored = []
    for s in _focus_pool(db):
        hit = wanted & _tokens(" ".join([s.name, s.one_liner, " ".join(s.tags or []), s.signal]))
        if hit:
            scored.append((len(hit), s, sorted(hit)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [public_card(s, matched) for _, s, matched in scored[:limit]]


def match_card(m: Match) -> dict:
    s = m.startup
    o = m.outreach
    confirmed = bool(m.user.email_verified_at) if m.user else False  # founder emails need a confirmed account
    return {
        "match_id": m.id,
        "name": s.name,
        "domain": s.domain,
        "one_liner": s.one_liner,
        "stage": s.stage,
        "location": s.location,
        "signal": s.signal,
        "source": s.source,
        "source_url": s.source_url,
        "score": m.score,
        "reason": m.reason,
        "status": m.status,
        "founders": [{k: f.get(k) for k in ("name", "title", "profile_url", "linkedin", "x", "linkedin_search") if f.get(k)}
                     for f in (s.founders or [])][:4],
        "contacts": (s.contacts or [])[:5] if confirmed else [],
        "jobs": (s.jobs or [])[:6],
        "open_roles": len(s.jobs or []),  # the badge counts all of them, the card lists the first six
        # what the research agent found (shared per startup); shown so the user can check the draft's facts
        "research": {k: (s.research or {}).get(k) for k in ("what_they_do", "recent", "hooks")} if s.research else None,
        "pow": m.pow,
        "confirmed": confirmed,
        "outreach": None if o is None else {
            "status": o.status, "to_email": o.to_email, "to_name": o.to_name, "to_confidence": o.to_confidence,
            "dm_note": o.dm_note, "subject": o.subject, "body": o.body,
            "sent_at": o.sent_at.isoformat() if o.sent_at else None, "followups_sent": o.followups_sent,
            "next_followup_at": o.next_followup_at.isoformat() if o.next_followup_at and o.followups_enabled else None,
            "followup_draft": o.followup_draft,
            "replied_at": o.replied_at.isoformat() if o.replied_at else None, "reply_snippet": o.reply_snippet,
            # the user sends it themselves: prefilled Gmail / mail-app links
            "compose": compose_links(o.to_email, o.subject, o.body) if o.body and o.to_email else None,
            "followup_compose": compose_links(o.to_email, "Re: " + o.subject.removeprefix("Re: "), o.followup_draft)
            if o.followup_draft and o.to_email else None,
            "error": o.error,
        },
    }
