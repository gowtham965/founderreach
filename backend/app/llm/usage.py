"""Record every model call's token usage, so AI costs are measured rather than estimated.

Rates are USD per million tokens from https://platform.claude.com/docs/en/about-claude/pricing
(fetched 2026-10-25). Check that page when they change; only the report uses them.
"""
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
INR_PER_USD = 88.0
IST = timezone(timedelta(hours=5, minutes=30))

# model: (input, 5-minute cache write, cache read, output)
RATES = {
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),  # prompts up to 100K tokens
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
    # OpenAI, from https://developers.openai.com/api/docs/pricing (fetched 2026-10-27); no cache-write premium
    "gpt-6.1-sol": (2.00, 2.00, 0.10, 10.00),
    "gpt-6-luna": (0.10, 0.10, 0.01, 0.50),
    "gpt-6.1-terra": (1.00, 1.00, 0.05, 5.00),
    "gpt-6-astra": (10.00, 10.00, 1.00, 50.00),
}


# Whose AI work this is: set around each user-triggered piece of work (chat, draft, research, ideas...),
# so every model call is charged to that user and checked against their daily cap. (id, exempt-from-cap)
_who: ContextVar[tuple[int, bool] | None] = ContextVar("founderreach_ai_user", default=None)


@contextmanager
def for_user(user):
    """Charge the AI calls made inside this block to `user` (admins are recorded but never capped)."""
    from app.auth import is_admin
    token = _who.set((user.id, is_admin(user)) if user is not None and user.id else None)
    try:
        yield
    finally:
        _who.reset(token)


def charged(fn):
    """Decorator for service functions shaped f(db, user, ...): their AI calls are charged to that user."""
    import functools

    @functools.wraps(fn)
    def wrapper(db, user, *args, **kwargs):
        with for_user(user):
            return fn(db, user, *args, **kwargs)
    return wrapper


def record(task: str, model: str, usage, stop_reason: str | None = "") -> None:
    """Store one call's usage. Never let bookkeeping break the user's request."""
    try:
        from app.db import SessionLocal
        from app.models import LLMUsage

        with SessionLocal() as db:
            usd = cost_usd(model, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "cache_creation_input_tokens", 0) or 0,
                           getattr(usage, "cache_read_input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0) or 0
            _spend["inr"] += usd * INR_PER_USD  # keep the budget checks current between refreshes
            who = _who.get()
            today = datetime.now(IST).date().isoformat()
            if who and (who[0], today) in _user_spend:
                _user_spend[(who[0], today)]["inr"] += usd * INR_PER_USD
            db.add(LLMUsage(
                user_id=who[0] if who else None,
                task=task or "", model=model,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                stop_reason=stop_reason or "",
            ))
            db.commit()
    except Exception:
        log.exception("could not record LLM usage for %s", task)


def cost_usd(model: str, inp: int, write: int, read: int, out: int) -> float | None:
    r = RATES.get(model)
    if r is None:
        return None
    return (inp * r[0] + write * r[1] + read * r[2] + out * r[3]) / 1_000_000


# ---------------------------------------------------------------- site-wide daily budget
class BudgetExceeded(RuntimeError):
    """Today's AI spend reached DAILY_AI_BUDGET_INR: AI pauses until midnight IST."""


class UserBudgetExceeded(RuntimeError):
    """This user's AI spend today reached MAX_AI_INR_PER_USER_PER_DAY: their AI pauses until midnight IST."""

    message = ("You've reached today's AI limit for your account, so drafting, research and chat pause until midnight. "
               "Your matches, drafts and the buttons on your cards (I sent it, they replied) all still work.")


_user_spend: dict[tuple[int, str], dict] = {}


def user_spend_inr(user_id: int) -> float:
    """One user's AI spend since midnight IST, refreshed from the usage table at most once a minute."""
    day = datetime.now(IST).date().isoformat()
    entry = _user_spend.get((user_id, day))
    if entry is None or time.time() - entry["at"] > 60:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import LLMUsage

        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        with SessionLocal() as db:
            rows = db.scalars(select(LLMUsage).where(LLMUsage.user_id == user_id, LLMUsage.created_at >= start)).all()
        total = sum(cost_usd(u.model, u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens) or 0 for u in rows)
        if len(_user_spend) > 5000:  # yesterday's entries
            _user_spend.clear()
        entry = _user_spend[(user_id, day)] = {"inr": total * INR_PER_USD, "at": time.time()}
    return entry["inr"]


_spend = {"day": "", "inr": 0.0, "at": 0.0}
_alerted = {"day": ""}


def today_spend_inr() -> float:
    """AI spend since midnight IST, refreshed from the usage table at most once a minute."""
    day = datetime.now(IST).date().isoformat()
    if _spend["day"] != day or time.time() - _spend["at"] > 60:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import LLMUsage

        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        with SessionLocal() as db:
            rows = db.scalars(select(LLMUsage).where(LLMUsage.created_at >= start)).all()
        total = sum(cost_usd(u.model, u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens) or 0 for u in rows)
        _spend.update(day=day, inr=total * INR_PER_USD, at=time.time())
    return _spend["inr"]


def check_budget() -> None:
    """Called before every paid AI call. Raises BudgetExceeded once today's cap is reached."""
    from app.config import get_settings

    who = _who.get()
    user_cap = get_settings().max_ai_inr_per_user_per_day
    if who and not who[1] and user_cap > 0 and user_spend_inr(who[0]) >= user_cap:
        raise UserBudgetExceeded(UserBudgetExceeded.message)
    cap = get_settings().daily_ai_budget_inr
    if cap <= 0:
        return
    spent = today_spend_inr()
    if spent < cap:
        return
    if _alerted["day"] != _spend["day"]:
        _alerted["day"] = _spend["day"]
        from app.services import alerts
        alerts.problem(f"Daily AI budget reached: Rs {spent:.0f} of Rs {cap:.0f}. AI is paused until midnight IST. "
                       "Raise DAILY_AI_BUDGET_INR in Render if this is real use.")
    raise BudgetExceeded(f"daily AI budget of Rs {cap:.0f} reached")
