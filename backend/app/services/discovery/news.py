"""Startup funding news (RSS feeds): the AI picks out the company behind each story; articles fill in city and round."""
import logging
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
import httpx

from app.config import get_settings
from app.llm import get_llm
from app.services.contacts import normalize_domain
from app.services.discovery.articles import fill_from_article

log = logging.getLogger(__name__)

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
            out.append(fill_from_article(row))
    return out


# Headlines rarely say where a startup is or which round it was; the article's first paragraphs nearly always do
