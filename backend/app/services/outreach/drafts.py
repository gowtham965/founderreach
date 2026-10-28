"""Writing the email (with research and the house style), the LinkedIn note, and the user's own edits."""
import logging

from sqlalchemy.orm import Session

from app.llm import get_llm
from app.llm import usage
from app.models import Match, Outreach, User
from app.services import research as research_svc
from app.services.humanize import humanize
from app.services.outreach import enrich
from app.services.outreach.common import OutreachError, _owned_match, _use_draft_quota, require_confirmed, with_linkedin
from app.services.outreach.prompts import DM_SYSTEM, DRAFT_PROMPT, draft_system
from app.services.profile import profile_brief

log = logging.getLogger(__name__)


@usage.charged
def draft(db: Session, user: User, match_id: int, instructions: str = "") -> Outreach:
    m = _owned_match(db, user, match_id)
    require_confirmed(user)
    o = m.outreach
    if o and o.status not in ("draft", "cancelled", "failed"):
        raise OutreachError(f"Email to {m.startup.name} is already {o.status}; it can't be redrafted")
    _use_draft_quota(db, user)
    from app.services import billing  # billing imports this module
    billing.use_draft(db, user)
    if o is None:
        o = Outreach(match_id=m.id, user_id=user.id)
        m.outreach = o  # keep the cached relationship in sync (sessions don't expire on commit)
        db.add(o)
    s = enrich.enrich_startup(db, m.startup)
    research = research_svc.research_startup(db, s)  # something specific and recent to open with
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
                            roles=enrich._roles_line(s), research=research_svc.brief_for_prompt(research),
                            recipient=o.to_name or "the founder", current=current, instructions=extra),
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


def write_dm(user: User, m: Match) -> str:
    s = m.startup
    founder = next(iter(s.founders or []), {})
    try:
        data = get_llm().complete_json(
            DM_SYSTEM,
            f"Candidate:\n{profile_brief(user.profile)}\nName: {user.name}\n\nFounder: {founder.get('name', 'the founder')}\n"
            f"Startup: {s.name}: {s.one_liner}\nWhy now: {s.signal}\nWhy it fits: {m.reason}\n{enrich._roles_line(s)}\n"
            'Return JSON: {"note": str}',
            task="dm_note",
        )
        note = humanize("", data.get("note", ""), kind="LinkedIn note")["body"].strip()
    except Exception as exc:
        log.warning("DM note failed: %s", exc)
        return ""
    return note[:300]


@usage.charged
def make_dm(db: Session, user: User, match_id: int) -> Outreach:
    m = _owned_match(db, user, match_id)
    if m.outreach is None:
        raise OutreachError("Draft the email first")
    from app.services import billing
    billing.require_access(db, user)
    enrich.enrich_startup(db, m.startup)
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
