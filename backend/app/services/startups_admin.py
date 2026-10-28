"""Admin → Startups: browse, search, edit, add, hide, CSV export/import and data quality for the startup database.

Hand edits are remembered in Startup.edited, so the morning refresh never overwrites them; hidden startups never
reach users (matches, the public pages). Contacts follow the enrichment shape ({email, name, confidence, source}).
"""
import csv
import io
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Match, Startup, aware, now
from app.services.contacts import normalize_domain
from app.services.discovery import in_focus, normalize_stage

EDITABLE = ("name", "domain", "one_liner", "stage", "location", "signal", "tags", "source_url")
CSV_FIELDS = ("name", "domain", "one_liner", "stage", "location", "signal", "source", "source_url",
              "founder_name", "founder_title", "founder_email", "tags", "hidden", "discovered_at")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GOOD_EMAIL = ("verified", "public", "likely", "user")  # a founder address we'd send to


class StartupError(ValueError):
    pass


def _founder_email(s: Startup) -> dict | None:
    return next((c for c in (s.contacts or []) if c.get("name") and c.get("confidence") in GOOD_EMAIL), None)


def city_of(location: str | None) -> str:
    """'HSR Layout, Bengaluru, KA, India' → 'Bengaluru'; '' → '?'."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    parts = [p for p in parts if p.lower() not in ("india", "in") and not (len(p) == 2 and p.isupper())]
    city = parts[-1] if parts else "?"
    return "Bengaluru" if city.lower() in ("bangalore", "bengaluru") else city


def quality(s: Startup) -> list[str]:
    """What's missing: no_website, no_founder, no_founder_email, not_checked."""
    out = []
    if not s.domain:
        out.append("no_website")
    if not s.founders:
        out.append("no_founder")
    if not _founder_email(s):
        out.append("no_founder_email")
    if not s.enriched_at:
        out.append("not_checked")
    return out


def row(s: Startup, matches: int = 0) -> dict:
    email = _founder_email(s)
    return {
        "id": s.id, "name": s.name, "domain": s.domain, "one_liner": s.one_liner, "stage": s.stage, "location": s.location,
        "signal": s.signal, "source": s.source, "source_url": s.source_url, "tags": s.tags or [],
        "founders": s.founders or [], "contacts": s.contacts or [], "jobs": s.jobs or [], "research": s.research,
        "founder_email": email["email"] if email else None,
        "in_focus": in_focus(s.location, s.stage), "hidden": bool(s.hidden), "hidden_reason": s.hidden_reason or "",
        "edited": s.edited or [], "quality": quality(s), "matches": matches, "open_roles": len(s.jobs or []),
        "jobs_checked_at": aware(s.jobs_checked_at).isoformat() if s.jobs_checked_at else None,
        "discovered_at": aware(s.discovered_at).isoformat() if s.discovered_at else None,
        "enriched_at": aware(s.enriched_at).isoformat() if s.enriched_at else None,
    }


def listing(db: Session, q: str = "", stage: str = "", city: str = "", source: str = "", focus: str = "",
            issue: str = "", hidden: str = "", sort: str = "new", page: int = 1, per_page: int = 50, hiring: str = "") -> dict:
    everything = db.scalars(select(Startup)).all()
    match_counts = dict(db.execute(select(Match.startup_id, func.count()).group_by(Match.startup_id)).all())
    ql = q.strip().lower()

    def keep(s: Startup) -> bool:
        if hidden != "all" and bool(s.hidden) != (hidden == "only"):
            return False
        if ql and ql not in " ".join([s.name, s.domain or "", s.one_liner, s.signal, " ".join(s.tags or [])]).lower():
            return False
        if stage and normalize_stage(s.stage) != stage:
            return False
        if city and city.lower() not in (s.location or "").lower():
            return False
        if source and s.source != source:
            return False
        if focus == "yes" and not in_focus(s.location, s.stage):
            return False
        if focus == "no" and in_focus(s.location, s.stage):
            return False
        if hiring and bool(s.jobs) != (hiring == "yes"):
            return False
        return not issue or issue in quality(s)

    rows = [s for s in everything if keep(s)]
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    key = {"new": lambda s: aware(s.discovered_at) or old, "name": lambda s: s.name.lower(),
           "matches": lambda s: match_counts.get(s.id, 0), "roles": lambda s: len(s.jobs or [])}.get(sort, lambda s: aware(s.discovered_at) or old)
    rows.sort(key=key, reverse=sort != "name")
    page = max(1, page)
    visible = [s for s in everything if not s.hidden]
    return {
        "total": len(rows), "page": page, "per_page": per_page,
        "rows": [row(s, match_counts.get(s.id, 0)) for s in rows[(page - 1) * per_page: page * per_page]],
        "summary": {
            "all": len(visible), "in_focus": sum(in_focus(s.location, s.stage) for s in visible),
            "hidden": len(everything) - len(visible),
            "added_7d": sum(aware(s.discovered_at) >= now() - timedelta(days=7) for s in visible if s.discovered_at),
            "hiring": sum(bool(s.jobs) for s in visible),
            "hiring_focus": sum(bool(s.jobs) for s in visible if in_focus(s.location, s.stage)),
            "issues": dict(Counter(i for s in visible for i in quality(s))),
            "sources": dict(Counter(s.source or "?" for s in visible)),
            "stages": dict(Counter(normalize_stage(s.stage) or "?" for s in visible)),
            "cities": dict(Counter(city_of(s.location) for s in visible).most_common(12)),
        },
    }


def _clean(field: str, value):
    if field == "domain":
        d = normalize_domain(value or "")
        if value and not d:
            raise StartupError("That website doesn't look right. Use something like acme.ai")
        return d
    if field == "tags":
        items = value if isinstance(value, list) else str(value or "").split(",")
        return [str(t).strip()[:40] for t in items if str(t).strip()][:12]
    if field == "stage":
        return normalize_stage(value) or ""
    if field == "name":
        name = str(value or "").strip()[:255]
        if not name:
            raise StartupError("A startup needs a name")
        return name
    return str(value or "").strip()[:2000]


def update(db: Session, startup_id: int, data: dict) -> Startup:
    s = db.get(Startup, startup_id)
    if s is None:
        raise StartupError("No such startup")
    edited = set(s.edited or [])
    for field in EDITABLE:
        if field in data:
            value = _clean(field, data[field])
            if field == "domain" and value and value != s.domain and db.scalar(select(Startup).where(Startup.domain == value)):
                raise StartupError(f"Another startup already uses {value}")
            setattr(s, field, value)
            edited.add(field)
    if any(k in data for k in ("founder_name", "founder_title", "founder_email")):
        first = (s.founders or [{}])[0]
        _set_founder(s, data.get("founder_name", first.get("name")), data.get("founder_title", first.get("title")),
                     data.get("founder_email"))
        edited.add("founders")
    if "hidden" in data:
        s.hidden = bool(data["hidden"])
        s.hidden_reason = str(data.get("hidden_reason") or "").strip()[:255] if s.hidden else None
    s.edited = sorted(edited)
    db.commit()
    return s


def _set_founder(s: Startup, name, title, email) -> None:
    """Put a hand-entered founder (and their email) first; the email is marked as set by you."""
    name, email = (name or "").strip()[:120], (email or "").strip().lower()[:255]
    if email and not EMAIL_RE.match(email):
        raise StartupError("That founder email doesn't look right")
    if name:
        rest = [f for f in (s.founders or []) if f.get("name") != name]
        s.founders = [{"name": name, "title": (title or "Founder").strip()[:80], "source": "admin"}] + rest
    if email:
        rest = [c for c in (s.contacts or []) if c.get("email") != email]
        s.contacts = [{"email": email, "name": name or (s.founders or [{}])[0].get("name", ""), "title": (title or "Founder"),
                       "confidence": "user", "source": "admin"}] + rest


def create(db: Session, data: dict, source: str = "manual") -> Startup:
    name = _clean("name", data.get("name"))
    domain = _clean("domain", data.get("domain")) if data.get("domain") else None
    if domain and db.scalar(select(Startup).where(Startup.domain == domain)):
        raise StartupError(f"{domain} is already in the database")
    s = Startup(name=name, domain=domain, source=source, founders=[], contacts=None,
                **{f: _clean(f, data.get(f)) for f in ("one_liner", "stage", "location", "signal", "tags", "source_url") if data.get(f)})
    db.add(s)
    db.flush()
    if data.get("founder_name") or data.get("founder_email"):
        _set_founder(s, data.get("founder_name"), data.get("founder_title"), data.get("founder_email"))
    s.edited = sorted({f for f in EDITABLE if data.get(f)} | ({"founders"} if data.get("founder_name") else set()))
    db.commit()
    return s


def export_csv(db: Session) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    w.writeheader()
    for s in db.scalars(select(Startup).order_by(Startup.name)):
        f = (s.founders or [{}])[0]
        email = _founder_email(s)
        w.writerow({
            "name": s.name, "domain": s.domain or "", "one_liner": s.one_liner, "stage": s.stage, "location": s.location,
            "signal": s.signal, "source": s.source, "source_url": s.source_url, "founder_name": f.get("name", ""),
            "founder_title": f.get("title", ""), "founder_email": email["email"] if email else "",
            "tags": ", ".join(s.tags or []), "hidden": "yes" if s.hidden else "",
            "discovered_at": aware(s.discovered_at).date().isoformat() if s.discovered_at else "",
        })
    return out.getvalue()


def import_csv(db: Session, text: str) -> dict:
    """Add new startups and fill in existing ones (matched by website, else name). Needs a 'name' column."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = {c.strip().lower() for c in (reader.fieldnames or [])}
    if "name" not in cols:
        raise StartupError("The CSV needs a 'name' column (plus any of: " + ", ".join(CSV_FIELDS[1:-1]) + ")")
    added = updated = 0
    errors = []
    for n, raw in enumerate(reader, start=2):
        if n > 5001:
            errors.append("Stopped at 5,000 rows")
            break
        data = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not data.get("name"):
            continue
        try:
            domain = normalize_domain(data.get("domain") or "")
            existing = (db.scalar(select(Startup).where(Startup.domain == domain)) if domain else None) \
                or db.scalar(select(Startup).where(func.lower(Startup.name) == data["name"].lower()))
            if existing:
                fill = {k: v for k, v in data.items() if k in EDITABLE and v and not getattr(existing, k)}
                if data.get("founder_email") or data.get("founder_name"):
                    fill.update({k: data.get(k) for k in ("founder_name", "founder_title", "founder_email") if data.get(k)})
                if data.get("hidden", "").lower() in ("yes", "true", "1"):
                    fill["hidden"] = True
                if fill:
                    update(db, existing.id, fill)
                    updated += 1
            else:
                create(db, data, source="import")
                added += 1
        except StartupError as exc:
            db.rollback()
            errors.append(f"Row {n} ({data.get('name')}): {exc}")
    return {"added": added, "updated": updated, "errors": errors[:50]}
