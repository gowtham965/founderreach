"""The admin tab's numbers: totals for 1/7/30 days, a 30-day daily series, the outreach funnel, users, payments.

Read-only. "Active" means sent at least one chat message that day (the closest per-day signal we record).
"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import is_admin
from app.config import get_settings
from app.llm.usage import cost_usd
from app.models import ChatMessage, LLMUsage, Match, Outreach, Payment, Profile, User, aware, now
from app.llm.usage import INR_PER_USD


def _day(dt) -> str:
    return aware(dt).date().isoformat()


def _status(u: User, at) -> str:
    if is_admin(u):
        return "admin"
    if u.pass_until and aware(u.pass_until) > at:
        return f"pass till {aware(u.pass_until):%d %b}"
    if u.trial_started_at:
        return "trial" if aware(u.trial_started_at) + timedelta(days=get_settings().trial_days) > at else "trial ended"
    return "not started"


def overview(db: Session, search: str = "") -> dict:
    t = now()
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    paid = db.scalars(select(Payment).where(Payment.applied_at.is_not(None))).all()
    chats = db.execute(select(ChatMessage.user_id, ChatMessage.created_at)
                       .where(ChatMessage.role == "user", ChatMessage.created_at >= t - timedelta(days=30))).all()
    usage = db.scalars(select(LLMUsage).where(LLMUsage.created_at >= t - timedelta(days=30))).all()

    def window(days: int) -> dict:
        since = t - timedelta(days=days)
        cost = sum(cost_usd(u.model, u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens) or 0
                   for u in usage if aware(u.created_at) >= since)
        return {
            "signups": sum(aware(u.created_at) >= since for u in users),
            "active": len({uid for uid, at in chats if aware(at) >= since}),
            "paying": len({p.user_id for p in paid if aware(p.applied_at) >= since}),
            "revenue_inr": sum(p.amount_paise for p in paid if aware(p.applied_at) >= since) // 100,
            "ai_cost_inr": round(cost * INR_PER_USD, 2),
        }

    days = [(t - timedelta(days=i)).date().isoformat() for i in range(29, -1, -1)]
    signups, active = defaultdict(int), defaultdict(set)
    for u in users:
        signups[_day(u.created_at)] += 1
    for uid, at in chats:
        active[_day(at)].add(uid)
    series = [{"day": d, "signups": signups[d], "active": len(active[d])} for d in days]

    # funnel, all time: each step counts users who reached it
    ready = set(db.scalars(select(Profile.user_id).where(Profile.summary != "")))
    matched = set(db.scalars(select(Match.user_id)))
    drafted = set(db.scalars(select(Outreach.user_id)))
    sent = set(db.scalars(select(Outreach.user_id).where(Outreach.sent_at.is_not(None))))
    replied = set(db.scalars(select(Outreach.user_id).where(Outreach.replied_at.is_not(None))))
    payers = {p.user_id for p in paid}
    funnel = [{"step": s, "users": n} for s, n in (
        ("Signed up", len(users)), ("Added a resume", len(ready)), ("Got matches", len(matched)),
        ("Drafted an email", len(drafted)), ("Sent an email", len(sent)), ("Got a reply", len(replied)), ("Paid", len(payers)))]

    per_user = defaultdict(lambda: {"matches": 0, "drafts": 0, "sent": 0, "replies": 0, "paid_inr": 0})
    for uid, n in db.execute(select(Match.user_id, func.count()).group_by(Match.user_id)):
        per_user[uid]["matches"] = n
    for o in db.scalars(select(Outreach)):
        per_user[o.user_id]["drafts"] += 1
        per_user[o.user_id]["sent"] += o.sent_at is not None
        per_user[o.user_id]["replies"] += o.replied_at is not None
    for p in paid:
        if p.user_id:
            per_user[p.user_id]["paid_inr"] += p.amount_paise // 100
    q = search.strip().lower()
    rows = [{
        "id": u.id, "email": u.email, "name": u.name,
        "joined": aware(u.created_at).isoformat(),
        "via": "WhatsApp" if u.phone else ("Google" if u.google_sub else "Email"),
        "confirmed": bool(u.email_verified_at),
        "last_seen": aware(u.last_seen_at).isoformat() if u.last_seen_at else None,
        "status": _status(u, t), **per_user[u.id],
    } for u in users if not q or q in (u.email or "").lower() or q in (u.name or "").lower()][:500]

    payments = [{"at": aware(p.created_at).isoformat(), "email": p.email, "plan": p.plan, "amount_inr": p.amount_paise // 100,
                 "state": p.state, "gateway": p.provider or "phonepe", "order": p.merchant_order_id}
                for p in db.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(100))]
    return {"windows": {"today": window(1), "7d": window(7), "30d": window(30)}, "series": series, "funnel": funnel,
            "users": rows, "users_total": len(users), "payments": payments}
