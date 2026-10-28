"""Bangalore Startup Map: the directory's homepage data, read weekly (facts only; credited and linked)."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from app.config import get_settings
from app.models import Startup
from app.services.contacts import normalize_domain

log = logging.getLogger(__name__)


_MAP_STAGES = {"seed": "seed", "pre-seed": "pre-seed"}


def _startup_map_rows(html: str) -> list[dict]:
    """The page is a Next.js app: the directory is JSON inside self.__next_f.push([1, "..."]) strings."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html)
    text = "".join(json.loads(c) for c in chunks)
    dec, seen, out = json.JSONDecoder(), set(), []
    for m in re.finditer(r'\{\s*"name"\s*:\s*"', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("slug") and "stage" in obj and obj["slug"] not in seen:
            seen.add(obj["slug"])
            out.append(obj)
    return out


def _startup_map_fresh(db_factory) -> bool:
    days = get_settings().startup_map_refresh_days
    with db_factory() as db:
        last = db.scalar(select(func.max(Startup.discovered_at)).where(Startup.source == "startup_map"))
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return bool(last and last > datetime.now(timezone.utc) - timedelta(days=days))


def fetch_startup_map() -> list[dict]:
    """Seed and pre-seed startups from Bangalore Startup Map. One request: the homepage carries the
    whole directory. Keeps facts only (name, site, stage, sector, area, founded, founder names)."""
    s = get_settings()
    if not s.startup_map_enabled:
        return []
    from app.db import SessionLocal

    if _startup_map_fresh(SessionLocal):
        log.info("startup map: read within %d days, skipping", s.startup_map_refresh_days)
        return []
    r = httpx.get(s.startup_map_url, timeout=30, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 FounderReach (weekly; credits and links back)"})
    r.raise_for_status()
    raw = _startup_map_rows(r.text)
    if not raw:
        log.warning("startup map: no companies found; the page layout may have changed")
    min_year = datetime.now(timezone.utc).year - s.startup_map_max_age_years
    base = s.startup_map_url.rstrip("/")
    out = []
    for c in raw:
        stage = _MAP_STAGES.get(str(c.get("stage") or "").lower())
        year = c.get("founded_year") if isinstance(c.get("founded_year"), int) else None
        if c.get("kind") != "startup" or not stage or (year and year < min_year):
            continue
        area = str(c.get("area") or "").strip()
        names = [n.strip() for n in str(c.get("founders") or "").split(",") if n.strip() and n.strip() != "$undefined"]
        out.append({
            "name": c.get("name", ""),
            "domain": normalize_domain(c.get("website")),
            "one_liner": str(c.get("tagline") or ""),
            "stage": stage,
            "location": "Bengaluru" if area.lower() in ("", "bengaluru", "bangalore") else f"{area}, Bengaluru",
            "tags": [t for t in (c.get("tags") or []) if isinstance(t, str)][:6] + ([c["sector"]] if c.get("sector") else []),
            "signal": f"{stage.capitalize()} stage" + (f", founded {year}" if year else ""),
            "source": "startup_map",
            "source_url": f"{base}/company/{c['slug']}",
            "founders": [{"name": n, "title": "Founder", "source": "startup_map"} for n in names],
        })
    return out
