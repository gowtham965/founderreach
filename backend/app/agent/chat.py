"""One chat turn: daily cap, trial check, history, then the tool loop until the model answers."""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.cards import card_refs
from app.agent.prompts import AI_UNAVAILABLE, SYSTEM, WHATSAPP_NOTE
from app.agent.tools import TOOLS, _run_tool
from app.auth import is_admin
from app.config import get_settings
from app.llm import get_llm
from app.llm import usage
from app.models import ChatMessage, User
from app.services import billing, discovery, profile as profile_svc

log = logging.getLogger(__name__)


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


def over_daily_chat_cap(db: Session, user: User) -> bool:
    """Counts today's message; True once the user is past MAX_CHATS_PER_DAY. Admins are never capped."""
    if is_admin(user):
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    used = (user.chats_used or 0) if user.chats_day == today else 0
    if used >= get_settings().max_chats_per_day:
        return True
    user.chats_day, user.chats_used = today, used + 1
    db.commit()
    return False


@usage.charged
def respond(db: Session, user: User, text: str, max_steps: int = 6, channel: str = "web") -> dict:
    db.add(ChatMessage(user_id=user.id, role="user", content=text))
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id, ChatMessage.unread.is_(True)).update({"unread": False})
    db.commit()

    try:
        billing.use_chat(db, user)
    except billing.PaywallError as exc:  # trial used up: answer with the offer instead of spending AI
        db.add(ChatMessage(user_id=user.id, role="assistant", content=str(exc)))
        db.commit()
        return {"reply": str(exc), "cards": [], "paywall": True}

    if over_daily_chat_cap(db, user):
        msg = (f"That's {get_settings().max_chats_per_day} messages today, the daily limit. The buttons on your cards "
               "(draft, I sent it, they replied) still work, and chat opens again tomorrow.")
        db.add(ChatMessage(user_id=user.id, role="assistant", content=msg))
        db.commit()
        return {"reply": msg, "cards": []}

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
    reply = ""
    try:
        llm = get_llm()
        for _ in range(max_steps):
            resp = llm.chat(system, messages, TOOLS, task="chat", cache_history=True)
            if not resp.tool_calls:
                reply = resp.text.strip()
                break
            messages.append({"role": "assistant", "content": resp.text, "tool_calls": [tc.as_dict() for tc in resp.tool_calls],
                             "raw": resp.raw})
            for tc in resp.tool_calls:
                result = _run_tool(db, user, tc.name, tc.input or {}, text, cards)
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": json.dumps(result, default=str)})
        else:
            reply = "I got a bit tangled there. Could you say that again?"
    except usage.UserBudgetExceeded as exc:  # this user's AI cost cap for today
        db.rollback()
        reply = str(exc)
    except Exception:  # missing key, outage, rate limit: answer anyway, never leave the message hanging
        log.exception("chat reply failed for user %s", user.id)
        db.rollback()
        reply = AI_UNAVAILABLE

    reply = reply or "Done."
    db.add(ChatMessage(user_id=user.id, role="assistant", content=reply, cards=card_refs(cards) or None))
    db.commit()
    return {"reply": reply, "cards": cards}
