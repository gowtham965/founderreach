"""The chat agent: one tool-using loop per user message.

The model decides what to do; the tools (and the outreach service under them)
enforce the rules. FounderReach never sends email: the user sends drafts from their own
inbox. Marking something sent or replied needs the user's own latest message to say so,
so a confused model can't rewrite their pipeline.
"""
import json
import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm import Tool, get_llm
from app.models import ChatMessage, Match, User
from app.services import discovery, matching, outreach, profile as profile_svc

log = logging.getLogger(__name__)

SYSTEM = """You are FounderReach, a job agent that helps people land interviews at early-stage
startups by emailing founders directly. You chat like a friendly, sharp recruiter: short
messages, plain words, no corporate tone, no walls of text.

How you work:
1. Learn about the user. If they have no profile, ask for their resume or LinkedIn PDF (they attach
   it with the paperclip) or a short description, plus what they want: roles, sectors, remote or
   in-office. Ask them to paste their LinkedIn profile link (open their profile and copy the
   address; in the phone app: Share profile → Copy link) and save it with
   update_profile(linkedin=...); it goes into their emails so founders can look them up. When you
   save it, show it back exactly as "Saved: <link> (tap to check it's you)" so typos get caught.
   You cannot open LinkedIn pages, so a link alone tells you nothing about their background: if
   that's all they share, say so and ask for the resume or LinkedIn PDF (LinkedIn: profile → More
   → Save to PDF). Save preferences with update_profile. Right now FounderReach only covers {focus};
   if they ask for other cities or later stages, say so plainly and that more is coming.
2. Find startups that fit with find_matches. Present the top few with the one-line reason.
3. When they pick one, call draft_email and show the draft. They can ask for edits (call
   draft_email again with their instructions) or set a founder email with set_recipient.
4. You never send email. The user sends the draft themselves from their own email: the draft
   card has "Open in Gmail" and "Open in my email app" buttons that prefill everything. If they
   say "send", explain that and point them to the button. Mention when the address is a guess.
5. When they say they sent it, call mark_sent. You'll remind them to follow up after a few days
   with a fresh short draft (best sent as a reply to their first email); mark_followup_sent when
   they've sent that. When they say a founder replied, call mark_replied: reminders stop.
   cancel_followups stops reminders without a reply.

Founder emails come with a confidence: verified, public (published by the company), likely,
inbox (shared founders@/hello@ inbox) or guess. Say plainly when it's a guess or inbox and offer
the alternatives: another address, a LinkedIn/X note they send themselves (write_dm), or applying
through the startup's open role if one fits (share the link). You never message anyone on LinkedIn.

Be honest: you can't guarantee interviews, and guessed emails may bounce. Refer to startups by
name and #match_id so the user can say "draft #12". Today's user profile:
{profile}"""

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
    Tool("find_matches", "Find new startups that fit the user now (from the discovered pool).", {"type": "object", "properties": {}}),
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

_SAID_SO = re.compile(r"\b(sent|send|mailed|emailed|done|replied|reply|responded|answered|wrote back|got back)\b", re.I)
_NEGATE = re.compile(r"\b(don'?t|do not|not yet|haven'?t|didn'?t|no reply|wait|hold|never)\b", re.I)


def user_said_so(text: str) -> bool:
    """Pipeline changes (sent / replied) only when the user's own latest message says so."""
    return bool(_SAID_SO.search(text)) and not _NEGATE.search(text)


def _history(db: Session, user: User, keep: int = 30, step: int = 10) -> list[dict]:
    """The last 30-39 messages. The window's start moves in steps of 10, not one message per turn,
    so the conversation prefix stays byte-identical (and cached) for ten turns at a time."""
    total = db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.user_id == user.id)) or 0
    start = max(0, (total - keep) // step * step)
    rows = db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id)
                      .order_by(ChatMessage.id).offset(start)).all()
    msgs = [{"role": r.role, "content": r.content} for r in rows]
    while msgs and msgs[0]["role"] != "user":  # providers want the first turn to be the user's
        msgs.pop(0)
    merged: list[dict] = []
    for m in msgs:  # merge same-role neighbours (e.g. two notifications in a row)
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(dict(m))
    return merged


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
            new = matching.find_matches(db, user)
            items = [matching.match_card(m) for m in new]
            if items:
                cards.append({"type": "matches", "items": items})
            return {"ok": True, "new_matches": [{k: c[k] for k in ("match_id", "name", "one_liner", "stage", "signal", "reason", "score")} for c in items]
                    or "No new fits in today's pool. New startups are added every morning."}
        if name == "list_matches":
            q = select(Match).where(Match.user_id == user.id)
            if args.get("status"):
                q = q.where(Match.status == args["status"])
            items = [matching.match_card(m) for m in db.scalars(q.order_by(Match.score.desc()).limit(20))]
            return {"ok": True, "matches": [{k: c[k] for k in ("match_id", "name", "status", "reason")} for c in items]}
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


WHATSAPP_NOTE = """

The user is chatting on WhatsApp, not the website. Files come in via 📎 → Document (not a
paperclip button). WhatsApp formatting: *bold*, _italic_, no markdown headings or tables. Keep
messages short. They open their dashboard by typing "dashboard". Drafts come with an "Open in
Gmail" link they tap to send it themselves. Startup cards and drafts are shown to them automatically after
your message, so don't repeat a draft's full text."""


def respond(db: Session, user: User, text: str, max_steps: int = 6, channel: str = "web") -> dict:
    db.add(ChatMessage(user_id=user.id, role="user", content=text))
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id, ChatMessage.unread.is_(True)).update({"unread": False})
    db.commit()

    profile_svc.get_or_create_profile(db, user)
    just_saved = ""
    if "linkedin.com/in/" in text.lower():  # a pasted profile link is saved directly, not left to the model
        try:
            url = profile_svc.update_profile(db, user, linkedin=text).links["linkedin"]
            just_saved = f'[Note for this turn: just saved their LinkedIn link {url}. Show it back: "Saved: {url} (tap to check it\'s you)".]'
        except ValueError:
            pass
    system = SYSTEM.format(profile=profile_svc.profile_brief(user.profile),
                           focus=discovery.focus_label()) + (WHATSAPP_NOTE if channel == "whatsapp" else "")
    messages = _history(db, user)
    if just_saved and messages and messages[-1]["role"] == "user":
        # per-turn notes ride in the user turn, so the cached system prompt stays byte-stable
        messages[-1]["content"] += "\n\n" + just_saved
    cards: list[dict] = []
    llm = get_llm()
    reply = ""
    for _ in range(max_steps):
        resp = llm.chat(system, messages, TOOLS, task="chat", cache_history=True)
        if not resp.tool_calls:
            reply = resp.text.strip()
            break
        messages.append({"role": "assistant", "content": resp.text, "tool_calls": [tc.as_dict() for tc in resp.tool_calls]})
        for tc in resp.tool_calls:
            result = _run_tool(db, user, tc.name, tc.input or {}, text, cards)
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": json.dumps(result, default=str)})
    else:
        reply = "I got a bit tangled there. Could you say that again?"

    reply = reply or "Done."
    db.add(ChatMessage(user_id=user.id, role="assistant", content=reply, cards=card_refs(cards) or None))
    db.commit()
    return {"reply": reply, "cards": cards}


def card_refs(cards: list[dict]) -> list[dict]:
    """What to remember about a reply's cards: just match ids. A startup drafted then marked sent
    in one turn is remembered once, at its last position."""
    refs, seen = [], set()
    for c in reversed(cards):
        if c.get("type") == "matches":
            refs.append({"type": "matches", "ids": [i["match_id"] for i in c.get("items", [])]})
        elif c.get("item") and c["item"]["match_id"] not in seen:
            seen.add(c["item"]["match_id"])
            refs.append({"type": "draft", "id": c["item"]["match_id"]})
    return list(reversed(refs))


def rebuild_cards(db: Session, user: User, refs: list[dict] | None) -> list[dict]:
    """Cards for a stored reply, from current data. Matches that no longer exist are dropped."""
    out = []
    for ref in refs or []:
        if ref.get("type") == "matches":
            items = [matching.match_card(m) for m in (db.get(Match, i) for i in ref.get("ids", []))
                     if m is not None and m.user_id == user.id]
            if items:
                out.append({"type": "matches", "items": items})
        else:
            m = db.get(Match, ref.get("id"))
            if m is not None and m.user_id == user.id:
                out.append({"type": "draft", "item": matching.match_card(m)})
    return out
