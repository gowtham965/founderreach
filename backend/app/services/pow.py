"""Proof-of-work agent: "I made this for you" gets replies a CV doesn't.

For one match it suggests 3 small things the candidate could make for that startup in 1-3 hours (a product
teardown, a bug list, a mini demo, a data look, growth ideas...), grounded in the candidate's skills and the
research brief. The user picks one; the agent writes a starter (outline and first draft) that says plainly
what they must check by using the product. Once they've made it and shared a link, the email is redrafted
to mention it in one line.

Kept on the match as {"ideas": [...], "chosen": int, "starter": str, "link": str}.
"""
import re

from sqlalchemy.orm import Session

from app.llm import get_llm
from app.models import Match, User
from app.services import billing
from app.services import outreach
from app.services import research as research_svc
from app.services.profile import profile_brief

IDEAS_SYSTEM = """You help a job seeker stand out to a startup founder by making something small and useful for that
startup before asking for anything. Suggest exactly 3 ideas that:
- fit this candidate's skills (an engineer builds or debugs; a designer redesigns a flow; a marketer or ops person
  analyses, plans or writes),
- can be done in 1-3 hours from public things only (their website, public app, docs, reviews), with no insider access,
- connect to something specific the startup is working on, from the facts given,
- end in something shareable (a doc, a short video, a repo, a mockup).
Vary them: not three teardowns. No praise, no generic "research the company". Use only facts given."""

IDEAS_PROMPT = """Candidate:
{profile}

Startup: {name} ({domain}): {one_liner}
Why now: {signal}
Why they fit: {reason}
{roles}
{research}

Return JSON: {{"ideas": [{{"title": str (under 8 words), "what": str (what to make, 1-2 sentences),
  "why": str (why this founder would care, tied to a fact above), "effort": str (e.g. "about 2 hours"),
  "kind": "teardown|bug list|mini demo|data analysis|growth plan|content|redesign|other"}}]}}"""

STARTER_SYSTEM = """You write a starter for a small piece of work a job seeker will make for a startup, so they can finish it
in 1-3 hours. Give: a one-line goal, the steps to do it, and a first draft of the finished piece (an outline with the
first sections written, or the plan for a demo with the key parts). Anything that depends on actually using their
product must be written as a placeholder in [square brackets] for the candidate to fill in: never invent what their
app does, its bugs, numbers or users. End with "Check before sharing:" and 2-4 things to verify. Plain text, no
markdown headings, under 450 words."""

STARTER_PROMPT = """Candidate:
{profile}

Startup: {name} ({domain}): {one_liner}
{research}

Make a starter for this idea:
{idea}

Return JSON: {{"starter": str}}"""

URL_RE = re.compile(r"^https://[^\s/$.?#][^\s]*\.[^\s]+$")


def _context(db: Session, m: Match) -> dict:
    s = m.startup
    research = research_svc.research_startup(db, s)
    return {"name": s.name, "domain": s.domain or "", "one_liner": s.one_liner or "", "signal": s.signal or "",
            "reason": m.reason or "", "roles": outreach._roles_line(s), "research": research_svc.brief_for_prompt(research)}


def suggest(db: Session, user: User, match_id: int) -> Match:
    m = outreach._owned_match(db, user, match_id)
    billing.require_access(db, user)
    data = get_llm().complete_json(IDEAS_SYSTEM, IDEAS_PROMPT.format(profile=profile_brief(user.profile), **_context(db, m)),
                                   task="pow_ideas", max_tokens=3000)
    ideas = []
    for it in (data or {}).get("ideas", [])[:3]:
        if isinstance(it, dict) and str(it.get("title", "")).strip() and str(it.get("what", "")).strip():
            ideas.append({k: str(it.get(k, "")).strip()[:400] for k in ("title", "what", "why", "effort", "kind")})
    if not ideas:
        raise outreach.OutreachError("Couldn't come up with good ideas for this one. Try again in a minute.")
    m.pow = {"ideas": ideas}
    db.commit()
    return m


def start(db: Session, user: User, match_id: int, index: int) -> Match:
    m = outreach._owned_match(db, user, match_id)
    ideas = (m.pow or {}).get("ideas") or []
    if not 0 <= index < len(ideas):
        raise outreach.OutreachError("Pick one of the ideas first")
    billing.require_access(db, user)
    outreach._use_draft_quota(db, user)  # a starter is as much writing as a draft
    idea = ideas[index]
    ctx = _context(db, m)
    data = get_llm().complete_json(STARTER_SYSTEM, STARTER_PROMPT.format(
        profile=profile_brief(user.profile), name=ctx["name"], domain=ctx["domain"], one_liner=ctx["one_liner"],
        research=ctx["research"], idea=f"{idea['title']}: {idea['what']} (why: {idea['why']})"), task="pow_starter", max_tokens=4000)
    starter = str((data or {}).get("starter", "")).strip()
    if not starter:
        raise outreach.OutreachError("Couldn't write the starter. Try again in a minute.")
    m.pow = {**(m.pow or {}), "chosen": index, "starter": starter[:6000]}
    db.commit()
    return m


def attach(db: Session, user: User, match_id: int, link: str) -> Match:
    """They made it: save the link and redraft the email to mention it in one line."""
    m = outreach._owned_match(db, user, match_id)
    link = link.strip()
    if not URL_RE.match(link) or len(link) > 500:
        raise outreach.OutreachError("Paste the full https:// link to what you made (Google Doc, Notion, Loom, GitHub…)")
    p = m.pow or {}
    if p.get("chosen") is None:
        raise outreach.OutreachError("Pick one of the ideas first")
    title = p["ideas"][p["chosen"]]["title"]
    m.pow = {**p, "link": link}
    db.commit()
    outreach.draft(db, user, match_id, instructions=(
        f"I made something for them: \"{title}\", at {link}. Mention it in one short, plain sentence with the link, "
        "right before the ask. Don't oversell it."))
    return m
