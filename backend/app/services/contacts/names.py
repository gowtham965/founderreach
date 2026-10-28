"""Founder names: the public YC company page, the startup's own About/Team text (via the AI), LinkedIn search links."""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from urllib.parse import quote_plus

from app.services.contacts import fetch

log = logging.getLogger(__name__)
YC_FOUNDER_RE = re.compile(r"/founders/\d+-([a-z0-9-]+)")

def linkedin_search_url(name: str, company: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(f'{name} {company}')}"


def yc_founders(company_name: str, yc_url: str = "", domain: str | None = None) -> list[dict]:
    """Founder names from the public YC company page.

    With a known YC link, use it. Otherwise the page address is guessed from the company name,
    which can belong to a different company with the same name: then the YC page's website must
    match the startup's domain, or we'd draft emails to the wrong founders."""
    guessed = "ycombinator.com/companies/" not in (yc_url or "")
    url = yc_url if not guessed else \
        f"https://www.ycombinator.com/companies/{re.sub(r'[^a-z0-9]+', '-', company_name.lower()).strip('-')}"
    r = fetch._get(url)
    if r is None:
        return []
    page = r.text
    people: dict[str, dict] = {}
    # Inertia-style pages embed props as JSON in data-page; take founders from there when present
    m = re.search(r'data-page="([^"]+)"', page)
    props = None
    if m:
        try:
            props = json.loads(html_lib.unescape(m.group(1)))
        except ValueError:
            props = None
    if guessed:
        site = _find_text(props, "website") if props is not None else None
        if not domain or not site or fetch.normalize_domain(site) != domain:
            return []
    if props is not None:
        try:
            for f in _find_key(props, "founders") or []:
                name = f.get("full_name") or f.get("name")
                if name:
                    people[name] = {"name": name, "title": f.get("title") or "Founder", "source": "yc",
                                    "linkedin": f.get("linkedin_url") or "", "x": f.get("twitter_url") or ""}
        except (ValueError, AttributeError, TypeError):
            pass
    if not people:  # fallback: founder profile links like /founders/360041-zeno-rocha
        for slug in dict.fromkeys(YC_FOUNDER_RE.findall(page)):
            name = " ".join(w.capitalize() for w in slug.split("-"))
            people[name] = {"name": name, "title": "Founder", "source": "yc",
                            "profile_url": f"https://www.ycombinator.com/founders/{slug}"}
    return list(people.values())[:4]


def _find_text(obj, key):
    """First string value stored under `key`, anywhere in nested JSON (`_find_key` only finds lists)."""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), str) and obj[key]:
            return obj[key]
        for v in obj.values():
            found = _find_text(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_text(v, key)
            if found:
                return found
    return None


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], list):
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found:
                return found
    return None


def names_from_site_text(text: str, company: str) -> list[dict]:
    """Ask the LLM to pull founder names out of the About/Team page text."""
    if len(text) < 200 or not re.search(r"(?i)found|ceo|team", text):
        return []
    from app.llm import get_llm

    try:
        rows = get_llm().complete_json(
            "Extract the founders of the company from its website text. Only people explicitly described as "
            "founder, co-founder, CEO or CTO. Never guess.",
            f"Company: {company}\nWebsite text:\n{text[:6000]}\n\nReturn JSON: [{{\"name\": str, \"title\": str}}]",
            task="extract_founders",
        )
    except Exception as exc:
        log.debug("founder extraction failed: %s", exc)
        return []
    return [{"name": r["name"], "title": r.get("title") or "Founder", "source": "website"}
            for r in rows or [] if isinstance(r, dict) and r.get("name")][:4]
