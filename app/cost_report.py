"""What the AI actually cost, from recorded usage.   python -m app.cost_report [days] [inr_per_usd]"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.llm.usage import cost_usd
from app.models import LLMUsage


def report(days: int = 7, inr_per_usd: float = 88.0) -> str:
    init_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = defaultdict(lambda: [0, 0, 0, 0, 0, 0.0, set()])  # calls, in, write, read, out, usd, models
    cut_off = 0
    with SessionLocal() as db:
        for u in db.scalars(select(LLMUsage).where(LLMUsage.created_at >= since)):
            r = rows[u.task or "?"]
            r[0] += 1
            r[1] += u.input_tokens
            r[2] += u.cache_write_tokens
            r[3] += u.cache_read_tokens
            r[4] += u.output_tokens
            r[5] += cost_usd(u.model, u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens) or 0
            r[6].add(u.model)
            cut_off += u.stop_reason == "max_tokens"
    if not rows:
        return f"No AI calls recorded in the last {days} days (demo mode with the fake AI records nothing)."
    lines = [f"AI usage, last {days} days (₹{inr_per_usd:g} per $)", "",
             f"{'task':<18}{'calls':>6}{'input':>10}{'cache wr':>10}{'cache rd':>10}{'output':>9}{'cached %':>9}{'cost ₹':>10}"]
    total = 0.0
    for task, (n, i, w, rd, o, usd, models) in sorted(rows.items(), key=lambda kv: -kv[1][5]):
        cached = rd / (i + w + rd) * 100 if (i + w + rd) else 0
        lines.append(f"{task:<18}{n:>6}{i:>10}{w:>10}{rd:>10}{o:>9}{cached:>8.0f}%{usd * inr_per_usd:>10.2f}")
        total += usd
    lines += ["", f"Total: ₹{total * inr_per_usd:.2f} (${total:.4f})"]
    if cut_off:
        lines.append(f"⚠ {cut_off} replies were cut off by max_tokens (wasted calls).")
    return "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    print(report(int(args[0]) if args else 7, float(args[1]) if len(args) > 1 else 88.0))
