"""Reading a funding article's lead for the city and round that headlines leave out."""
import json
import re

import httpx

from app.config import get_settings
from app.services.discovery.focus import _csv, in_focus, normalize_stage


_BASED_RE = re.compile(r"\b([A-Z][a-zA-Z]+)[-\s](?:based|headquartered)\b|\bheadquartered in ([A-Z][a-zA-Z]+)")
# Cities a startup can be "X-based" in; anything else ("UPI-based", "AI-based") is not a place
_CITIES = {"bengaluru", "bangalore", "mumbai", "delhi", "gurugram", "gurgaon", "noida", "pune", "hyderabad", "chennai",
           "kolkata", "ahmedabad", "jaipur", "kochi", "chandigarh", "indore", "coimbatore", "mysuru", "mysore", "bhubaneswar",
           "lucknow", "goa", "thiruvananthapuram", "trivandrum", "surat", "nagpur", "vadodara", "singapore", "dubai", "london",
           "delaware", "california", "texas", "york"}
_PRE_SEED_RE = re.compile(r"\bpre[-\s]seed\b", re.I)
_SEED_RE = re.compile(r"\bseed (?:round|funding|investment|capital)\b", re.I)
_LATER_RE = re.compile(r"\b(?:pre-)?series [a-f]\b", re.I)


def _article_text(url: str) -> str:
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 FounderReach"})
        r.raise_for_status()
    except httpx.HTTPError:
        return ""
    # Most news sites embed the story for search engines (schema.org "articleBody"): the cleanest copy
    for m in re.finditer(r'"articleBody"\s*:\s*("(?:[^"\\]|\\.)*")', r.text):
        try:
            body = json.loads(m.group(1))
        except ValueError:
            continue
        if len(body) > 200:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:4000]
    paras = (re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;", " ", p)).strip()
             for p in re.findall(r"<p[^>]*>(.*?)</p>", r.text, re.S | re.I))
    # article sentences only: menus, sign-in prompts and scripts also sit in <p> tags on news sites
    body = [p for p in paras if len(p) >= 80 and ". " in p + " " and "{" not in p and "Sign in" not in p]
    return " ".join(body)[:4000]


def article_facts(text: str) -> dict:
    """{"location": "Bengaluru, India" | other city | None, "stage": "pre-seed" | "seed" | "later" | None} from article text."""
    facts = {"location": None, "stage": None}
    for m in _BASED_RE.finditer(text[:1500]):  # the startup's own "X-based" comes first; investors' come later
        city = m.group(1) or m.group(2)
        if city.lower() in _CITIES:
            facts["location"] = "Bengaluru, India" if city.lower() in ("bengaluru", "bangalore") else city
            break
    first = text[:700]  # the lead names this round; past rounds come later in the story
    hits = [(m.start(), stage) for rx, stage in ((_PRE_SEED_RE, "pre-seed"), (_LATER_RE, "later"), (_SEED_RE, "seed"))
            if (m := rx.search(first))]
    if hits:
        facts["stage"] = min(hits)[1]
    return facts


def fill_from_article(row: dict) -> dict:
    """Fill a news row's missing city or round from its article, only when that could put it in focus."""
    stage = normalize_stage(row.get("stage"))
    missing_city, missing_stage = not row.get("location"), stage in ("", "unknown")
    if not (missing_city or missing_stage) or not row.get("source_url"):
        return row
    stages = _csv(get_settings().focus_stages)
    if not missing_stage and stages and stage not in stages:
        return row  # a Series B story stays out whatever the city
    if not missing_city and not in_focus(row["location"], "", stages=[]):
        return row  # a Mumbai startup stays out whatever the round
    facts = article_facts(_article_text(row["source_url"]))
    if missing_city and facts["location"]:
        row["location"] = facts["location"]
    if missing_stage and facts["stage"] in ("pre-seed", "seed"):
        row["stage"] = facts["stage"]
    return row
