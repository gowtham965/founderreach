"""Build the shared pool of startups that just raised, launched or started hiring.

Sources:
  * YC companies currently hiring (yc-oss public JSON mirror)
  * Startup/funding news RSS feeds → LLM extracts the company behind each story
Add more by writing a function that returns dicts with the Startup fields and
listing it in SOURCES.

Only startups inside the focus (FOCUS_CITIES, FOCUS_STAGES) are kept. Unknown city or
stage means out: a wrong match costs the user more than a missing one.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import get_llm
from app.models import Startup
from app.services.contacts import normalize_domain

log = logging.getLogger(__name__)


def _csv(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def normalize_stage(stage: str | None) -> str:
    s = re.sub(r"[\s_]+", " ", (stage or "").strip().lower())
    if s in ("pre seed", "preseed", "pre-seed", "angel"):
        return "pre-seed"
    if s in ("seed", "seed round", "seed stage"):
        return "seed"
    return s


def in_focus(location: str | None, stage: str | None, cities: list[str] | None = None,
             stages: list[str] | None = None) -> bool:
    s = get_settings()
    cities = _csv(s.focus_cities) if cities is None else cities
    stages = _csv(s.focus_stages) if stages is None else stages
    if cities and not any(c in (location or "").lower() for c in cities):
        return False
    if stages and normalize_stage(stage) not in stages:
        return False
    return True


def focus_label() -> str:
    """Plain words for the current focus, e.g. "pre-seed and seed startups in Bengaluru"."""
    s = get_settings()
    stages, cities = _csv(s.focus_stages), _csv(s.focus_cities)
    what = (" and ".join(stages) + " startups") if stages else "startups"
    # bengaluru/bangalore are one city; show the first spelling of each
    where = cities[0].title() if cities else ""
    return f"{what} in {where}" if where else what


def _yc_stage(c: dict) -> str:
    """YC's own label is just Early/Growth; an Early company from a recent batch is seed stage."""
    m = re.search(r"(\d{4})", c.get("batch") or "")
    recent = m and int(m.group(1)) >= datetime.now(timezone.utc).year - get_settings().yc_seed_batch_years
    if (c.get("stage") or "").lower() == "early" and recent:
        return "seed"
    return (c.get("stage") or "").lower()


def fetch_yc_hiring(limit: int = 2000) -> list[dict]:
    r = httpx.get(get_settings().yc_hiring_url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    companies = sorted(r.json(), key=lambda c: c.get("launched_at") or 0, reverse=True)[:limit]
    out = []
    for c in companies:
        out.append({
            "name": c.get("name", ""),
            "domain": normalize_domain(c.get("website")),
            "one_liner": c.get("one_liner") or (c.get("long_description") or "")[:240],
            "stage": _yc_stage(c),
            # city first ("Bengaluru, KA, India"); regions are too coarse to filter on
            "location": c.get("all_locations") or ", ".join(c.get("regions") or []),
            "tags": (c.get("tags") or []) + ([c["industry"]] if c.get("industry") else []),
            "signal": f"YC {c.get('batch', '')}, team of {c.get('team_size') or '?'}, hiring now",
            "source": "yc",
            "source_url": c.get("url") or "",
        })
    return out


EXTRACT_SYSTEM = """You turn startup news headlines into structured leads for job seekers.
Only include stories about one specific startup that raised money, launched a product,
or is hiring. Skip big public companies, opinion pieces and roundups. Never guess a
domain you are not confident about; use null instead. For location give the city the
startup is headquartered in, as the story states it (e.g. "Bengaluru, India"); use null
if the story doesn't say. Never guess a city or a funding stage."""

EXTRACT_PROMPT = """Stories:
{stories}

Return a JSON array, one item per qualifying story:
[{{"story": int, "name": str, "domain": str|null, "one_liner": str, "stage": "pre-seed|seed|series a|series b|series c|unknown",
   "location": str|null, "tags": [str], "signal": "what happened, e.g. raised $4M seed led by X",
   "founders": [{{"name": str, "title": str}}]}}]"""


def fetch_news(days: int = 3) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = []
    for url in [u.strip() for u in get_settings().news_feeds.split(",") if u.strip()]:
        # Fetch with httpx (bundled CA certs): feedparser's own urllib fetch fails TLS on
        # python.org macOS installs, which made every feed come back silently empty.
        try:
            r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 FounderReach"})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("feed %s failed: %s", url, exc)
            continue
        feed = feedparser.parse(r.content)
        for e in feed.entries[:60]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if ts and datetime.fromtimestamp(mktime(ts), timezone.utc) < cutoff:
                continue
            items.append({"title": e.get("title", ""), "summary": (e.get("summary") or "")[:400], "link": e.get("link", "")})
    out = []
    llm = get_llm()
    for i in range(0, len(items), 15):
        batch = items[i : i + 15]
        stories = "\n".join(f"[{n}] {it['title']} — {it['summary']}" for n, it in enumerate(batch))
        try:
            rows = llm.complete_json(EXTRACT_SYSTEM, EXTRACT_PROMPT.format(stories=stories), task="extract_startups", max_tokens=16000)
        except Exception as exc:
            log.warning("news extraction failed: %s", exc)
            continue
        for row in rows or []:
            n = row.get("story")
            row["source"] = "news"
            row["source_url"] = batch[n]["link"] if isinstance(n, int) and 0 <= n < len(batch) else ""
            row["domain"] = normalize_domain(row.get("domain"))
            out.append(row)
    return out


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


SOURCES = [fetch_yc_hiring, fetch_news, fetch_startup_map]


def upsert_startups(db: Session, rows: list[dict]) -> int:
    added = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        domain = row.get("domain")
        existing = None
        if domain:
            existing = db.scalar(select(Startup).where(Startup.domain == domain))
        if existing is None:
            existing = db.scalar(select(Startup).where(Startup.name == name))
        fields = {k: row.get(k) for k in ("one_liner", "stage", "location", "tags", "signal", "source", "source_url") if row.get(k)}
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            if row.get("founders") and not existing.founders:
                existing.founders = row["founders"]
            existing.discovered_at = datetime.now(timezone.utc)
        else:
            db.add(Startup(name=name, domain=domain, founders=row.get("founders") or [], **fields))
            added += 1
        db.flush()
    db.commit()
    return added


def run_discovery(db: Session) -> int:
    total = 0
    for source in SOURCES:
        try:
            rows = source()
            kept = [r for r in rows if in_focus(r.get("location"), r.get("stage"))]
            total += upsert_startups(db, kept)
            log.info("%s: %d rows, %d in focus", source.__name__, len(rows), len(kept))
        except Exception as exc:
            log.warning("discovery source %s failed: %s", source.__name__, exc)
    return total
