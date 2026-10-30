"""The chat assistant's tools: what it may do (TOOLS) and running one (_run_tool). The services enforce the rules;
marking an email sent or replied also needs the user's own latest message to say so (user_said_so)."""
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import Tool
from app.models import Match, User
from app.agents import proof_of_work as pow_svc, research as research_svc
from app.services import billing, jobs as jobs_svc, matching, outreach, profile as profile_svc

log = logging.getLogger(__name__)

TOOLS = [
    Tool("update_profile", "Save what the user told you about their background or what they want next.", {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "highlights": {"type": "array", "items": {"type": "string"}, "description": "Concrete wins, with numbers"},
            "target_roles": {"type": "array", "items": {"type": "string"}},
            "linkedin": {"type": "string", "description": "LinkedIn profile link the user shared (a bare username also works)"},
            "preferences": {"type": "object", "description": "e.g. stages, remote, locations, sectors, avoid, notes",
                            "properties": {"stages": {"type": "array", "items": {"type": "string"}},
                                           "remote": {"type": "string"},
                                           "locations": {"type": "array", "items": {"type": "string"}},
                                           "sectors": {"type": "array", "items": {"type": "string"}},
                                           "avoid": {"type": "array", "items": {"type": "string"}},
                                           "notes": {"type": "string"}}},
        },
    }),
    Tool("research_startup", "Research a matched startup (its site and funding news): what it does, recent launches and "
         "news with sources, and specific openers for an email. Use when the user asks about a startup.",
         {"type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("proof_of_work", "Suggest 3 small things the user could make for a matched startup in 1-3 hours (a teardown, "
         "a bug list, a mini demo...) to stand out. Use when they want to impress a founder or ask what to send.",
         {"type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("find_matches", "Find new startups that fit the user now (from the discovered pool).", {"type": "object", "properties": {}}),
    Tool("check_open_roles", "Look up a matched startup's open roles right now, from its job board and careers page "
         "(takes a few seconds). Use when the user asks whether a startup is hiring and its roles haven't been checked "
         "yet or may be stale.", {"type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("list_matches", "List the user's matches, optionally filtered by status (new, drafted, sent, replied, skipped).", {
        "type": "object", "properties": {"status": {"type": "string"}}}),
    Tool("draft_email", "Write or revise the email to a matched startup's founder.", {
        "type": "object",
        "properties": {"match_id": {"type": "integer"},
                       "instructions": {"type": "string", "description": "User's requested changes, if any"}},
        "required": ["match_id"]}),
    Tool("set_recipient", "Set the founder email (and name) for a draft when the user provides it.", {
        "type": "object",
        "properties": {"match_id": {"type": "integer"}, "email": {"type": "string"}, "name": {"type": "string"}},
        "required": ["match_id", "email"]}),
    Tool("mark_sent", "Record that the user sent the draft from their own email. Only when their latest message says they sent it.", {
        "type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("mark_replied", "Record that the founder replied. Stops follow-up reminders.", {
        "type": "object", "properties": {"match_id": {"type": "integer"},
                                         "snippet": {"type": "string", "description": "What they said, if the user shared it"}},
        "required": ["match_id"]}),
    Tool("mark_followup_sent", "Record that the user sent the reminded follow-up.", {
        "type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("write_dm", "Write a short LinkedIn/X note the user can send to the founder themselves (when no good email exists or they prefer a DM).", {
        "type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("skip_match","Skip a startup the user isn't interested in.", {
        "type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("cancel_followups", "Stop follow-up reminders for a sent email.", {
        "type": "object", "properties": {"match_id": {"type": "integer"}}, "required": ["match_id"]}),
    Tool("get_status", "Summary of the user's search: matches, sends, replies.", {"type": "object", "properties": {}}),
]

# Past tense only: "send it" / "did they reply?" ask for something, they don't report it.
_SAID_SO = re.compile(r"\b(sent|mailed|emailed|replied|responded|answered|wrote back|got back|heard back)\b", re.I)
_NEGATE = re.compile(r"\b(don'?t|do not|not yet|haven'?t|hasn'?t|didn'?t|no reply|not sent|wait|hold|never)\b", re.I)


def user_said_so(text: str) -> bool:
    """Pipeline changes (sent / replied) only when the user's own latest message reports it:
    some statement (not a question) has a past-tense 'sent'/'replied' and no negation."""
    for sentence in re.findall(r"[^.!?\n]+[.!?]?", text):
        s = sentence.strip()
        if s and not s.endswith("?") and _SAID_SO.search(s) and not _NEGATE.search(s):
            return True
    return False


def _roles(db: Session, match_id: int) -> dict:
    """What the assistant may say about hiring: the roles actually found, and when they were checked."""
    s = db.get(Match, match_id).startup
    roles = [f"{j['title']} ({j.get('location') or 'location n/a'}) {j.get('url', '')}".strip() for j in (s.jobs or [])[:5]]
    checked = s.jobs_checked_at.date().isoformat() if s.jobs_checked_at else None
    return {"open_roles": roles, "open_roles_total": len(s.jobs or []), "roles_checked": checked or "not checked yet"}


def _run_tool(db: Session, user: User, name: str, args: dict, latest_user_text: str, cards: list) -> dict:
    try:
        if name == "update_profile":
            try:
                p = profile_svc.update_profile(db, user, **args)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "profile": profile_svc.profile_brief(p)}
        if name == "find_matches":
            if not user.profile or not user.profile.is_ready():
                return {"ok": False, "error": "Profile incomplete: need a summary plus skills or target roles"}
            billing.require_access(db, user)
            new = matching.find_matches(db, user)
            items = [matching.match_card(m) for m in new]
            if items:
                cards.append({"type": "matches", "items": items})
            return {"ok": True, "new_matches": [{**{k: c[k] for k in ("match_id", "name", "one_liner", "stage", "signal", "reason", "score")},
                                                  **_roles(db, c["match_id"])} for c in items]
                    or "No new fits in today's pool. New startups are added every morning."}
        if name == "check_open_roles":
            m = db.get(Match, int(args["match_id"]))
            if m is None or m.user_id != user.id:
                return {"ok": False, "error": f"No match #{args['match_id']}"}
            billing.require_access(db, user)
            jobs_svc.refresh_startup(db, m.startup)
            cards.append({"type": "draft", "item": matching.match_card(m)})
            return {"ok": True, **_roles(db, m.id)}
        if name == "research_startup":
            m = db.get(Match, int(args["match_id"]))
            if m is None or m.user_id != user.id:
                return {"ok": False, "error": f"No match #{args['match_id']}"}
            billing.require_access(db, user)
            r = research_svc.research_startup(db, m.startup)
            return {"ok": True, "research": r or "Couldn't find anything specific on their site or in the news."}
        if name == "proof_of_work":
            m = pow_svc.suggest(db, user, int(args["match_id"]))
            cards.append({"type": "draft", "item": matching.match_card(m)})
            return {"ok": True, "ideas": m.pow["ideas"], "note": "The card shows these with a Start button for each."}
        if name == "list_matches":
            q = select(Match).where(Match.user_id == user.id)
            if args.get("status"):
                q = q.where(Match.status == args["status"])
            items = [matching.match_card(m) for m in db.scalars(q.order_by(Match.score.desc()).limit(20))]
            return {"ok": True, "matches": [{**{k: c[k] for k in ("match_id", "name", "status", "reason")}, **_roles(db, c["match_id"])}
                                            for c in items]}
        if name == "draft_email":
            o = outreach.draft(db, user, int(args["match_id"]), args.get("instructions", ""))
            cards.append({"type": "draft", "item": matching.match_card(o.match)})
            s = o.match.startup
            return {"ok": True, "to": o.to_email or "unknown: ask the user for the founder's email",
                    "email_confidence": o.to_confidence,
                    "other_addresses": [f"{c['email']} ({c['confidence']})" for c in (s.contacts or [])[1:4]],
                    "founders": [f.get("name") for f in s.founders or []],
                    "open_roles": [f"{j['title']} ({j.get('location', '')}) {j.get('url', '')}" for j in (s.jobs or [])[:4]],
                    "dm_note_ready": bool(o.dm_note),
                    "subject": o.subject, "body": o.body}
        if name == "write_dm":
            o = outreach.make_dm(db, user, int(args["match_id"]))
            cards.append({"type": "draft", "item": matching.match_card(o.match)})
            return {"ok": True, "note": o.dm_note}
        if name == "set_recipient":
            o = outreach.set_recipient(db, user, int(args["match_id"]), args["email"], args.get("name", ""))
            cards.append({"type": "draft", "item": matching.match_card(o.match)})
            return {"ok": True, "to": o.to_email}
        if name in ("mark_sent", "mark_replied", "mark_followup_sent"):
            if not user_said_so(latest_user_text):
                return {"ok": False, "error": "Not recorded: the user's latest message doesn't say so. Ask them."}
            mid = int(args["match_id"])
            o = (outreach.mark_sent(db, user, mid) if name == "mark_sent" else
                 outreach.mark_replied(db, user, mid, args.get("snippet", "")) if name == "mark_replied" else
                 outreach.mark_followup_sent(db, user, mid))
            cards.append({"type": "draft", "item": matching.match_card(o.match)})
            return {"ok": True, "status": o.status, "next_reminder": o.next_followup_at.isoformat() if o.next_followup_at else None}
        if name == "skip_match":
            outreach.skip(db, user, int(args["match_id"]))
            return {"ok": True}
        if name == "cancel_followups":
            outreach.cancel_followups(db, user, int(args["match_id"]))
            return {"ok": True}
        if name == "get_status":
            return {"ok": True, **outreach.stats(db, user)}
        return {"ok": False, "error": f"Unknown tool {name}"}
    except outreach.OutreachError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # keep the chat alive; the model can explain the failure
        log.exception("tool %s failed", name)
        db.rollback()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
