"""Drafts, the user's own sending, follow-up reminders and replies.

FounderReach never sends email. It writes the draft; the user sends it from their own
email (compose links prefill Gmail or their mail app) and tells us "I sent it". Follow-ups
are reminders with a fresh draft, and stop when the user says the founder replied.

Hard rules enforced here (not in the prompt):
  * founder emails and drafts need a confirmed email (stops throwaway accounts harvesting founders)
  * a per-user daily cap on drafts (MAX_DRAFTS_PER_DAY) keeps AI costs bounded
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import get_llm
from app.models import ChatMessage, Match, Outreach, Startup, User, aware, now
from app.services import contacts as contacts_svc
from app.services.humanize import humanize
from app.services.profile import profile_brief

log = logging.getLogger(__name__)

STYLE_FILE = Path(__file__).resolve().parents[2] / "prompts" / "email_style.md"


def style_guide() -> str:
    """Read fresh each time so edits to the style file apply without a restart."""
    try:
        return STYLE_FILE.read_text(encoding="utf-8")
    except OSError:
        return "Write short, specific, human emails. Use only facts given to you."


def draft_system() -> str:
    return "You write cold emails from a job seeker to a startup founder. Follow this style guide exactly.\n\n" + style_guide()


def followup_system() -> str:
    return ("Write a short follow-up in an existing email thread to a startup founder who has not replied. "
            "Follow the style guide's follow-up rules exactly.\n\n" + style_guide())

DRAFT_PROMPT = """Candidate:
{profile}
Candidate name: {name}

Startup: {startup} ({domain})
What they do: {one_liner}
Why now: {signal}
Why it fits: {reason}
{roles}
(If one open role clearly fits the candidate, name it in one short clause. Otherwise don't mention roles.)
Recipient: {recipient}
{current}{instructions}
Return JSON: {{"subject": str, "body": str}}"""

class OutreachError(Exception):
    pass


def with_linkedin(body: str, user: User) -> str:
    """Put the user's LinkedIn link under their sign-off: founders nearly always click it."""
    url = ((user.profile.links if user.profile else None) or {}).get("linkedin")
    if not url or url.split("://", 1)[-1].removeprefix("www.") in body:
        return body
    return f"{body}\n{url}"


def notify(db: Session, user_id: int, text: str) -> None:
    db.add(ChatMessage(user_id=user_id, role="assistant", content=text, unread=True))
    db.commit()


def _owned_match(db: Session, user: User, match_id: int) -> Match:
    m = db.get(Match, match_id)
    if m is None or m.user_id != user.id:
        raise OutreachError(f"No match #{match_id} for this user")
    return m


def require_confirmed(user: User) -> None:
    if not user.email_verified_at:
        raise OutreachError(f"Confirm your email first to see founder emails and drafts. We sent a link to {user.email}")


def _use_draft_quota(db: Session, user: User) -> None:
    """Count AI drafts per day (new drafts, rewrites and follow-ups); refuse past the cap."""
    today = datetime.now(timezone.utc).date().isoformat()
    used = user.drafts_used if user.drafts_day == today else 0
    if used >= get_settings().max_drafts_per_day:
        raise OutreachError("That's today's draft limit. More tomorrow morning")
    user.drafts_day, user.drafts_used = today, used + 1


def draft(db: Session, user: User, match_id: int, instructions: str = "") -> Outreach:
    m = _owned_match(db, user, match_id)
    require_confirmed(user)
    o = m.outreach
    if o and o.status not in ("draft", "cancelled", "failed"):
        raise OutreachError(f"Email to {m.startup.name} is already {o.status}; it can't be redrafted")
    _use_draft_quota(db, user)
    if o is None:
        o = Outreach(match_id=m.id, user_id=user.id)
        m.outreach = o  # keep the cached relationship in sync (sessions don't expire on commit)
        db.add(o)
    s = enrich_startup(db, m.startup)
    if not o.to_email:
        best = (s.contacts or [None])[0]
        if best:
            o.to_email, o.to_name, o.to_confidence = best["email"], best.get("name", ""), best["confidence"]
    current = f"\nCurrent draft (revise it):\nSubject: {o.subject}\n{o.body}\n" if o.body else ""
    extra = f"\nUser's instructions: {instructions}\n" if instructions else ""
    data = get_llm().complete_json(
        draft_system(),
        DRAFT_PROMPT.format(profile=profile_brief(user.profile), name=user.name or "", startup=s.name,
                            domain=s.domain or "", one_liner=s.one_liner, signal=s.signal, reason=m.reason,
                            roles=_roles_line(s), recipient=o.to_name or "the founder", current=current, instructions=extra),
        task="draft_email",
    )
    h = humanize(data.get("subject") or o.subject or f"Intro: {s.name}", data.get("body") or o.body)
    if h["tells_after"]:
        log.info("draft for match %s still has tells after humanize: %s", m.id, h["tells_after"])
    o.subject, o.body = h["subject"].strip(), with_linkedin(h["body"].strip(), user)
    if o.to_confidence in (None, "guess", "inbox"):
        o.dm_note = write_dm(user, m)
    o.status = "draft"
    m.status = "drafted"
    db.commit()
    return o


ENRICH_TTL = timedelta(days=14)


def enrich_startup(db: Session, s: Startup, force: bool = False) -> Startup:
    """Run the contact waterfall once per startup (cached for two weeks; shared by all users)."""
    if not force and s.enriched_at and aware(s.enriched_at) > now() - ENRICH_TTL:
        return s
    try:
        data = contacts_svc.enrich(s.name, s.domain, s.founders, s.source_url if s.source == "yc" else "")
    except Exception as exc:
        log.warning("enrichment failed for %s: %s", s.name, exc)
        return s
    s.founders = data["founders"] or s.founders or []
    s.contacts, s.jobs, s.github_org, s.enriched_at = data["contacts"], data["jobs"], data["github"], now()
    db.commit()
    return s


def _roles_line(s: Startup) -> str:
    jobs = s.jobs or []
    if not jobs:
        return "Open roles: none listed publicly"
    return "Open roles: " + "; ".join(f"{j['title']} ({j.get('location') or 'location n/a'})" for j in jobs[:6])


DM_SYSTEM = """Write a LinkedIn connection note (or X DM) from a job seeker to a startup founder.
Hard limit 280 characters. One specific line about the startup, one concrete win of the
candidate's with its number, one small ask. No greeting fluff, no emojis, no hashtags, no
em dashes. Use only facts given. Sign with the first name."""


def write_dm(user: User, m: Match) -> str:
    s = m.startup
    founder = next(iter(s.founders or []), {})
    try:
        data = get_llm().complete_json(
            DM_SYSTEM,
            f"Candidate:\n{profile_brief(user.profile)}\nName: {user.name}\n\nFounder: {founder.get('name', 'the founder')}\n"
            f"Startup: {s.name}: {s.one_liner}\nWhy now: {s.signal}\nWhy it fits: {m.reason}\n{_roles_line(s)}\n"
            'Return JSON: {"note": str}',
            task="dm_note",
        )
        note = humanize("", data.get("note", ""), kind="LinkedIn note")["body"].strip()
    except Exception as exc:
        log.warning("DM note failed: %s", exc)
        return ""
    return note[:300]


def make_dm(db: Session, user: User, match_id: int) -> Outreach:
    m = _owned_match(db, user, match_id)
    if m.outreach is None:
        raise OutreachError("Draft the email first")
    enrich_startup(db, m.startup)
    m.outreach.dm_note = write_dm(user, m)
    db.commit()
    return m.outreach


def set_recipient(db: Session, user: User, match_id: int, email: str, name: str = "") -> Outreach:
    m = _owned_match(db, user, match_id)
    if m.outreach is None or m.outreach.status != "draft":
        raise OutreachError("Draft the email first")
    m.outreach.to_email, m.outreach.to_name = email.strip(), name.strip()
    m.outreach.to_confidence = "user"
    db.commit()
    return m.outreach


def edit_draft(db: Session, user: User, match_id: int, subject: str | None, body: str | None) -> Outreach:
    m = _owned_match(db, user, match_id)
    if m.outreach is None or m.outreach.status != "draft":
        raise OutreachError("No editable draft")
    if subject:
        m.outreach.subject = subject
    if body:
        m.outreach.body = body
    db.commit()
    return m.outreach


def compose_links(to: str, subject: str, body: str) -> dict:
    """Prefilled 'new message' links: the user's own Gmail (web or app) and their default mail app."""
    gmail = "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body})
    mailto = f"mailto:{quote(to)}?" + urlencode({"subject": subject, "body": body}, quote_via=quote)
    return {"gmail": gmail, "mailto": mailto}


def mark_sent(db: Session, user: User, match_id: int) -> Outreach:
    """The user sent the draft from their own email."""
    m = _owned_match(db, user, match_id)
    o = m.outreach
    if o is None or o.status != "draft" or not o.body:
        raise OutreachError("There's no draft for that startup yet")
    days = _followup_days()
    o.status, o.sent_at, o.error = "sent", now(), ""
    o.next_followup_at = o.sent_at + timedelta(days=days[0]) if days else None
    m.status = "sent"
    db.commit()
    return o


def mark_replied(db: Session, user: User, match_id: int, snippet: str = "") -> Outreach:
    m = _owned_match(db, user, match_id)
    o = m.outreach
    if o is None or o.status not in ("sent", "replied"):
        raise OutreachError("Mark the email as sent first")
    o.status, o.replied_at, o.reply_snippet = "replied", now(), snippet[:500]
    o.next_followup_at, o.followup_draft = None, None
    m.status = "replied"
    db.commit()
    return o


def mark_followup_sent(db: Session, user: User, match_id: int) -> Outreach:
    m = _owned_match(db, user, match_id)
    o = m.outreach
    if o is None or o.status != "sent" or not o.followup_draft:
        raise OutreachError("There's no follow-up waiting for that startup")
    days = _followup_days()
    o.followups_sent += 1
    o.followup_draft = None
    nxt = days[o.followups_sent] if o.followups_sent < len(days) else None
    o.next_followup_at = aware(o.sent_at) + timedelta(days=nxt) if nxt else None
    db.commit()
    return o


def skip(db: Session, user: User, match_id: int) -> Match:
    m = _owned_match(db, user, match_id)
    if m.outreach and m.outreach.status in ("approved", "sent", "replied"):
        raise OutreachError("Already sent; stop the follow-up reminders instead")
    m.status = "skipped"
    if m.outreach:
        m.outreach.status = "cancelled"
    db.commit()
    return m


def cancel_followups(db: Session, user: User, match_id: int) -> Outreach:
    m = _owned_match(db, user, match_id)
    if m.outreach is None:
        raise OutreachError("Nothing was sent to that startup")
    m.outreach.followups_enabled = False
    m.outreach.next_followup_at = None
    m.outreach.followup_draft = None
    db.commit()
    return m.outreach


def _followup_days() -> list[int]:
    return [int(d) for d in get_settings().followup_days.split(",") if d.strip()]


def prepare_due_followups(db: Session) -> int:
    """Follow-ups that are due get a fresh draft and a reminder in the user's chat. Nothing is sent."""
    due = db.scalars(select(Outreach).where(Outreach.status == "sent", Outreach.followups_enabled.is_(True),
                                            Outreach.followup_draft.is_(None), Outreach.next_followup_at <= now()))
    count = 0
    for o in list(due):
        try:
            if o.followups_sent >= len(_followup_days()):
                o.next_followup_at = None
                db.commit()
                continue
            user = db.get(User, o.user_id)
            data = get_llm().complete_json(
                followup_system(),
                f"Candidate:\n{profile_brief(user.profile)}\nName: {user.name}\n\nOriginal email:\n{o.body}\n\n"
                f"Startup: {o.match.startup.name}: {o.match.startup.one_liner}\nReturn JSON: {{\"body\": str}}",
                task="followup",
            )
            o.followup_draft = humanize("", data["body"], kind="follow-up")["body"]
            db.commit()
            who = o.to_name or o.match.startup.name
            link = compose_links(o.to_email, "Re: " + o.subject.removeprefix("Re: "), o.followup_draft)["gmail"]
            notify(db, user.id, f"⏰ No reply from {who} at {o.match.startup.name} yet? Here's a short follow-up (#{o.match_id}):\n\n"
                                f"{o.followup_draft}\n\nBest sent as a reply to your first email so it stays in one thread. "
                                f"Or open it ready to send: {link}\n\n"
                                "Tell me when it's sent, or if they already replied and I'll stop reminding you.")
            count += 1
        except Exception:
            log.exception("follow-up draft failed for outreach %s", o.id)
            db.rollback()
    return count


def stats(db: Session, user: User) -> dict:
    rows = db.scalars(select(Outreach).where(Outreach.user_id == user.id)).all()
    return {
        "matches": db.scalar(select(func.count(Match.id)).where(Match.user_id == user.id)) or 0,
        "drafts": sum(o.status == "draft" for o in rows),
        "sent": sum(o.status in ("sent", "replied") for o in rows),
        "replies": sum(o.status == "replied" for o in rows),
        "followups_sent": sum(o.followups_sent for o in rows),
        "followups_due": sum(bool(o.followup_draft) for o in rows),
    }
