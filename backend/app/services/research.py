"""Research agent: before a draft, find something specific and recent about the startup.

Generic praise ("love what you're building") gets ignored; a line about what they shipped last month
gets replies. Two cheap model steps on the fast model:
  1. explore: read the homepage, pick up to 3 pages most likely to show what's new
     (blog posts, changelog, product, careers, press)
  2. brief: from those pages (and the funding article, for news startups) write what they do,
     recent facts with their source, and 2-3 opening hooks. Nothing that isn't on the pages.

The brief belongs to the startup, not the user, so it's shared by everyone drafting to it and kept
for RESEARCH_TTL. Fetching is limited to the startup's own site and the article it came from.
"""
import logging
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from app.llm import get_llm
from app.models import Startup, aware, now
from app.services import contacts

log = logging.getLogger(__name__)

RESEARCH_TTL = timedelta(days=7)
MAX_PAGES = 3
_SKIP = re.compile(r"(?i)(login|signin|sign-in|signup|sign-up|register|privacy|terms|cookie|legal|cart|checkout|"
                   r"\.(png|jpe?g|gif|svg|webp|ico|pdf|zip|css|js|xml|json)$)")
_A_RE = re.compile(r"""<a\s[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_IP_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$|^\[|localhost$|\.local$|\.internal$")

PICK_SYSTEM = """You research a startup so a job seeker can write its founder a specific, informed email.
From the startup's homepage and its list of links, choose up to 3 pages most likely to show what the
company shipped, announced or is building right now: recent blog posts, changelog, product pages, about,
careers, press. Prefer specific posts over index pages. Use only URLs from the list."""

PICK_PROMPT = """Startup: {name} ({domain})
Homepage text:
{home}

Links (url | link text):
{links}

Return JSON: {{"urls": [str]}}"""

BRIEF_SYSTEM = """You write a short research brief about a startup for a job seeker who will email its founder.
Use only facts stated in the sources below. Never guess dates, numbers, customers or names. If the sources
say little, return less: an empty list is better than a vague or invented item. Hooks must point at
something concrete and current (a launch, a feature, a funding round, a customer, a hire, a stated
problem), say it plainly, and contain no praise words (no "impressive", "love", "amazing")."""

BRIEF_PROMPT = """Startup: {name} ({domain})
Known: {one_liner} {signal}

Sources:
{sources}

Return JSON: {{"what_they_do": str (one sentence),
  "recent": [{{"fact": str, "source": str (one of the source URLs)}}] (max 4, most specific and recent first),
  "building": [str] (problems or tech they work on, max 4, a few words each),
  "hooks": [{{"line": str (one sentence a candidate could open with, built on one fact), "source": str}}] (max 3)}}"""


def _safe_host(host: str) -> bool:
    return bool(host) and "." in host and not _IP_RE.search(host.lower())


def _same_site(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _links(html: str, base: str, domain: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for href, label in _A_RE.findall(html):
        url = urljoin(base, href.strip()).split("?")[0].rstrip("/")
        if not url.startswith(("http://", "https://")) or not _same_site(url, domain) or _SKIP.search(url):
            continue
        if url in seen or url == base.rstrip("/"):
            continue
        seen.add(url)
        out.append((url, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label)).strip()[:80]))
        if len(out) >= 40:
            break
    return out


def _fetch_text(url: str, limit: int = 3500) -> str:
    r = contacts._get(url)
    return contacts._text(r.text[:400_000])[:limit] if r is not None else ""


def is_fresh(s: Startup) -> bool:
    return bool(s.research and s.researched_at and aware(s.researched_at) > now() - RESEARCH_TTL)


def research_startup(db: Session, s: Startup, force: bool = False) -> dict | None:
    """The startup's research brief, from cache or freshly made. Never raises: drafting goes on without it."""
    if not force and is_fresh(s):
        return s.research
    if not s.domain or not _safe_host(s.domain):
        return s.research
    try:
        brief = _research(s)
    except Exception:
        log.exception("research failed for %s", s.name)
        return s.research
    if brief:
        s.research, s.researched_at = brief, now()
        db.commit()
    return s.research


def _research(s: Startup) -> dict | None:
    base = f"https://{s.domain}"
    home = contacts._get(base)
    if home is None:
        return None
    home_html = home.text[:400_000]
    sources = [(base, contacts._text(home_html)[:3500])]
    llm = get_llm()

    links = _links(home_html, str(home.url), s.domain)
    if links:
        picked = llm.complete_json(PICK_SYSTEM, PICK_PROMPT.format(
            name=s.name, domain=s.domain, home=sources[0][1][:2000],
            links="\n".join(f"{u} | {t}" for u, t in links)), task="research_pick", max_tokens=2000)
        allowed = {u for u, _ in links}
        for url in [u for u in (picked or {}).get("urls", []) if u in allowed][:MAX_PAGES]:
            text = _fetch_text(url)
            if len(text) > 200:
                sources.append((url, text))
    if s.source == "news" and s.source_url and _safe_host(urlparse(s.source_url).hostname or ""):
        from app.services.discovery import _article_text  # the funding story itself, menus stripped
        article = _article_text(s.source_url)
        if article:
            sources.append((s.source_url, article[:3000]))

    brief = llm.complete_json(BRIEF_SYSTEM, BRIEF_PROMPT.format(
        name=s.name, domain=s.domain, one_liner=s.one_liner or "", signal=s.signal or "",
        sources="\n\n".join(f"[{u}]\n{t}" for u, t in sources)), task="research", max_tokens=3000)
    return _clean(brief, {u for u, _ in sources})


def _clean(brief, source_urls: set[str]) -> dict | None:
    """Keep only well-formed items whose source is one we actually read."""
    if not isinstance(brief, dict):
        return None
    def items(key, field, cap):
        out = []
        for it in brief.get(key) or []:
            if isinstance(it, dict) and str(it.get(field, "")).strip() and it.get("source") in source_urls:
                out.append({field: str(it[field]).strip()[:300], "source": it["source"]})
        return out[:cap]
    out = {
        "what_they_do": str(brief.get("what_they_do") or "").strip()[:300],
        "recent": items("recent", "fact", 4),
        "building": [str(b).strip()[:60] for b in (brief.get("building") or []) if str(b).strip()][:4],
        "hooks": items("hooks", "line", 3),
        "sources": sorted(source_urls),
    }
    return out if (out["what_they_do"] or out["recent"] or out["hooks"]) else None


def brief_for_prompt(research: dict | None) -> str:
    """The research as draft-prompt text."""
    if not research:
        return "Research: none found. Use only the facts above."
    lines = ["Research (facts from their site and news, with sources):"]
    if research.get("what_they_do"):
        lines.append(f"- What they do: {research['what_they_do']}")
    lines += [f"- Recent: {r['fact']}" for r in research.get("recent", [])]
    if research.get("building"):
        lines.append(f"- Working on: {', '.join(research['building'])}")
    lines += [f"- Possible opening: {h['line']}" for h in research.get("hooks", [])]
    lines.append("Open with ONE specific fact from this research, in your own words. Don't use any other claims about them.")
    return "\n".join(lines)
