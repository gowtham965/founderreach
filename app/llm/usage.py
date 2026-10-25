"""Record every model call's token usage, so AI costs are measured rather than estimated.

Rates are USD per million tokens from https://platform.claude.com/docs/en/about-claude/pricing
(fetched 2026-10-25). Check that page when they change; only the report uses them.
"""
import logging

log = logging.getLogger(__name__)

# model: (input, 5-minute cache write, cache read, output)
RATES = {
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),  # prompts up to 100K tokens
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
}


def record(task: str, model: str, usage, stop_reason: str | None = "") -> None:
    """Store one call's usage. Never let bookkeeping break the user's request."""
    try:
        from app.db import SessionLocal
        from app.models import LLMUsage

        with SessionLocal() as db:
            db.add(LLMUsage(
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
