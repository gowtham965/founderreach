"""Ops agent: a daily report for the admins (ADMIN_EMAILS).

Numbers for the last day (users, payments, AI spend, outreach, startups, background jobs), alerts from
fixed rules (failed jobs, sources gone quiet, stuck payments, spend spikes), and a short AI read of what
needs attention. The rules decide what's an alert; the AI only explains, so a bad AI day can't hide one.
Delivered to each admin's FounderReach chat, and by email when SMTP is set up.
"""
import json
import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import is_admin
from app.config import get_settings
from app.llm import get_llm
from app.llm.usage import INR_PER_USD, cost_usd
from app.models import ChatMessage, Job, JobRun, LLMUsage, Match, Outreach, Payment, Startup, User, aware, now

log = logging.getLogger(__name__)

SYSTEM = """You write the daily ops note for the founder of FounderReach, a small paid web app that helps job seekers
reach Bangalore startup founders. You get yesterday's numbers and rule-based alerts. Write at most 5 short bullets:
alerts first, explained in plain words with the likely cause and one concrete next step; then anything notable
(growth, first payment, a spike or drop). Use only the numbers given. No greetings, no filler, no praise.
If everything is quiet, say so in one bullet."""


def _ai_spend(db: Session, since) -> tuple[float, dict, int]:
    by_task: dict[str, float] = defaultdict(float)
    cut = 0
    for u in db.scalars(select(LLMUsage).where(LLMUsage.created_at >= since)):
        usd = cost_usd(u.model, u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens) or 0
        by_task[u.task or "?"] += usd * INR_PER_USD
        cut += u.stop_reason == "max_tokens"
    return sum(by_task.values()), dict(by_task), cut


def collect(db: Session, hours: int = 24) -> dict:
    t = now()
    since = t - timedelta(hours=hours)
    count = lambda q: db.scalar(q) or 0  # noqa: E731
    users_total = count(select(func.count(User.id)))
    spend, by_task, cut = _ai_spend(db, since)
    week_spend, _, _ = _ai_spend(db, t - timedelta(days=7))
    paid = db.scalars(select(Payment).where(Payment.applied_at >= since)).all()
    stuck = db.scalars(select(Payment).where(Payment.state == "PENDING", Payment.created_at < t - timedelta(hours=1),
                                             Payment.created_at >= t - timedelta(days=3))).all()
    runs = db.scalars(select(JobRun).where(JobRun.started_at >= t - timedelta(days=4)).order_by(JobRun.started_at)).all()
    return {
        "hours": hours,
        "users": {
            "total": users_total,
            "new": count(select(func.count(User.id)).where(User.created_at >= since)),
            "active": count(select(func.count(User.id)).where(User.last_seen_at >= since)),
            "with_pass": count(select(func.count(User.id)).where(User.pass_until > t)),
        },
        "payments": {"completed": len(paid), "revenue_inr": sum(p.amount_paise for p in paid) // 100,
                     "stuck_pending": [p.merchant_order_id for p in stuck]},
        "ai": {"spend_inr": round(spend, 2), "avg_day_7d_inr": round(week_spend / 7, 2),
               "by_task_inr": {k: round(v, 2) for k, v in sorted(by_task.items(), key=lambda kv: -kv[1])[:6]},
               "cut_off_replies": cut},
        "outreach": {
            "chat_messages": count(select(func.count(ChatMessage.id)).where(ChatMessage.role == "user", ChatMessage.created_at >= since)),
            "new_matches": count(select(func.count(Match.id)).where(Match.created_at >= since)),
            "drafts": count(select(func.count(LLMUsage.id)).where(LLMUsage.task == "draft_email", LLMUsage.created_at >= since)),
            "sent": count(select(func.count(Outreach.id)).where(Outreach.sent_at >= since)),
            "replies": count(select(func.count(Outreach.id)).where(Outreach.replied_at >= since)),
        },
        "startups": {"total": count(select(func.count(Startup.id))),
                     "added": count(select(func.count(Startup.id)).where(Startup.discovered_at >= since))},
        "roles": {"open": count(select(func.count(Job.id)).where(Job.closed_at.is_(None))),
                  "hiring_startups": count(select(func.count(func.distinct(Job.startup_id))).where(Job.closed_at.is_(None))),
                  "new": count(select(func.count(Job.id)).where(Job.first_seen_at >= since)),
                  "closed": count(select(func.count(Job.id)).where(Job.closed_at >= since))},
        "jobs": [{"name": r.name, "at": aware(r.started_at).isoformat(timespec="minutes"), "ok": r.ok, "detail": r.detail}
                 for r in runs if aware(r.started_at) >= since],
        "_runs": runs,  # for the rules; not shown
    }


def alerts(data: dict) -> list[str]:
    """What needs a look, by fixed rules."""
    out = []
    t = now()
    runs = data["_runs"]
    for r in runs:
        if not r.ok and aware(r.started_at) >= t - timedelta(hours=data["hours"]):
            out.append(f"Background job '{r.name}' failed at {aware(r.started_at):%d %b %H:%M} UTC: {json.dumps(r.detail)[:160]}")
    discovery = [r for r in runs if r.name == "discovery"]
    if not discovery or aware(discovery[-1].started_at) < t - timedelta(hours=26):
        out.append("The startup refresh hasn't run in over a day: the scheduler may be stopped.")
    last3 = [r.detail or {} for r in discovery[-3:]]
    if len(last3) == 3:
        for source in last3[-1]:
            if all(d.get(source) == 0 for d in last3):
                out.append(f"Source '{source}' added 0 startups in the last 3 refreshes.")
    from app.services import backup
    if backup.db_path() is not None:
        good = [r for r in runs if r.name == "backup" and r.ok]
        if not good or aware(good[-1].started_at) < t - timedelta(hours=26):
            out.append("No successful database backup in the last day. Use Pipeline → Download backup, and check the logs.")
    if data["payments"]["stuck_pending"]:
        out.append(f"{len(data['payments']['stuck_pending'])} payment(s) pending for over an hour: "
                   + ", ".join(data["payments"]["stuck_pending"][:5]))
    ai = data["ai"]
    if ai["spend_inr"] > 50 and ai["spend_inr"] > 2 * max(ai["avg_day_7d_inr"], 1):
        out.append(f"AI spend ₹{ai['spend_inr']:.0f} is over twice the 7-day average (₹{ai['avg_day_7d_inr']:.0f}/day).")
    if ai["cut_off_replies"]:
        out.append(f"{ai['cut_off_replies']} AI replies were cut off by the length limit (wasted calls).")
    return out


def render(data: dict, warn: list[str], note: str) -> str:
    u, p, ai, o, s = data["users"], data["payments"], data["ai"], data["outreach"], data["startups"]
    lines = [f"📊 Ops report, last {data['hours']}h", ""]
    if note:
        lines += [note, ""]
    elif warn:
        lines += ["Needs your attention:"] + [f"• {w}" for w in warn] + [""]
    else:
        lines += ["• All quiet: no failed jobs or alerts.", ""]
    lines += [
        f"Users: {u['new']} new, {u['active']} active, {u['total']} total, {u['with_pass']} with a pass",
        f"Payments: {p['completed']} completed, ₹{p['revenue_inr']} revenue",
        f"Outreach: {o['chat_messages']} chat messages, {o['new_matches']} matches, {o['drafts']} drafts, "
        f"{o['sent']} sent, {o['replies']} replies",
        f"Startups: +{s['added']} (total {s['total']})",
        f"Open roles: {data['roles']['open']} at {data['roles']['hiring_startups']} startups "
        f"(+{data['roles']['new']} new, {data['roles']['closed']} closed)",
        f"AI spend: ₹{ai['spend_inr']:.2f} (7-day avg ₹{ai['avg_day_7d_inr']:.2f}/day)"
        + (f"; top: {', '.join(f'{k} ₹{v:.2f}' for k, v in list(ai['by_task_inr'].items())[:3])}" if ai["by_task_inr"] else ""),
    ]
    jobs = data["jobs"]
    if jobs:
        lines.append("Jobs: " + "; ".join(f"{j['name']} {'✓' if j['ok'] else '✗'}"
                                           + (f" {j['detail']}" if j["name"] in ("discovery", "jobs") and j["detail"] else "") for j in jobs[-6:]))
    return "\n".join(lines)


def build(db: Session, hours: int = 24) -> str:
    data = collect(db, hours)
    warn = alerts(data)
    note = ""
    try:
        shown = {k: v for k, v in data.items() if not k.startswith("_")}
        resp = get_llm().complete_json(SYSTEM, f"Numbers:\n{json.dumps(shown, default=str)}\n\nAlerts:\n{json.dumps(warn)}\n\n"
                                               'Return JSON: {"bullets": [str]}', task="ops_report", max_tokens=2000)
        bullets = [str(b).strip() for b in (resp or {}).get("bullets", []) if str(b).strip()][:5]
        if bullets:
            # the rule alerts are always listed verbatim too, so the AI can't drop one
            note = "\n".join(f"• {b}" for b in bullets)
            if warn:
                note += "\n\nAlerts:\n" + "\n".join(f"• {w}" for w in warn)
    except Exception:
        log.exception("ops report: AI summary failed, sending numbers only")
    return render(data, warn, note)


def admins(db: Session) -> list[User]:
    emails = {e.strip().lower() for e in get_settings().admin_emails.split(",") if e.strip()}
    if not emails:
        return []
    return [u for u in db.scalars(select(User).where(func.lower(User.email).in_(emails))) if is_admin(u)]


def deliver(db: Session) -> int:
    """Send today's report to every admin: their chat, plus email when SMTP is configured."""
    people = admins(db)
    if not people:
        return 0
    report = build(db)
    from app.services import mailer
    for u in people:
        db.add(ChatMessage(user_id=u.id, role="assistant", content=report, unread=True))
        if mailer.is_configured():
            try:
                mailer.send(u.email, "FounderReach ops report", report, "<pre>" + report.replace("<", "&lt;") + "</pre>")
            except Exception:
                log.exception("ops report email to %s failed", u.email)
    db.commit()
    return len(people)
