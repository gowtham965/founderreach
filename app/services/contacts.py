"""Find a founder's email (and other ways to reach them) for a startup.

Waterfall, cheapest and most trustworthy first. Every step uses public data through
allowed routes; nothing logs in to or scrapes LinkedIn.

  1. Founder names     known names (news) + YC company page + the startup's own About/Team page
  2. Published emails  company-domain addresses on the startup's website or in its public
                       GitHub commits (GitHub API). Only @company addresses, never personal ones.
  3. Paid lookup       Hunter Email Finder, 1 credit, only if a key is set and steps 1-2 left a gap
  4. Pattern + verify  firstname@ / first.last@ / flast@, checked with Hunter Email Verifier if
                       a key is set (0.5 credit each, at most 3 tries)
  5. Shared inbox      founders@ / hello@ / jobs@ published on the site, else founders@ as a guess
  +  Open roles        public Ashby / Lever / Greenhouse job APIs (apply link to pair with the email)
  +  Profiles          YC founder page + a LinkedIn people-search link for a manual DM fallback

Confidence levels, best to worst:
  verified  a verifier confirmed the mailbox exists
  public    the founder's company address is published (site or GitHub)
  likely    Hunter found it, or the domain accepts all mail so it can't be confirmed
  inbox     a shared inbox the founders probably read
  guess     pattern guess, unverified
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

CONFIDENCE_RANK = {"verified": 5, "public": 4, "likely": 3, "inbox": 2, "guess": 1}
FOUNDER_WORDS = ("founder", "ceo", "cto", "chief executive", "co-founder")
INBOX_NAMES = ("founders", "founder", "hello", "hi", "team", "jobs", "careers", "hiring", "contact", "info")
SITE_PATHS = ["", "/about", "/team", "/company", "/about-us", "/careers", "/jobs", "/contact"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GITHUB_ORG_RE = re.compile(r"github\.com/([A-Za-z0-9-]{1,39})(?=[/\"'?#\s]|$)")
ATS_RES = {
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)"),
    "greenhouse": re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9._-]+)"),
}
YC_FOUNDER_RE = re.compile(r"/founders/\d+-([a-z0-9-]+)")
GITHUB_SKIP = {"features", "pricing", "about", "login", "sponsors", "orgs", "topics", "collections", "marketplace", "apps"}

_client: httpx.Client | None = None


def client() -> httpx.Client:
    """Shared HTTP client (tests swap this for one with a mock transport)."""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=12, follow_redirects=True,
                               headers={"User-Agent": "FounderReach/1.0 (+job search assistant)"})
    return _client


def _get(url: str, **kw) -> httpx.Response | None:
    try:
        r = client().get(url, **kw)
        return r if r.status_code == 200 else None
    except httpx.HTTPError as exc:
        log.debug("GET %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------- helpers
def normalize_domain(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    s = url_or_domain.strip().lower()
    if "://" not in s:
        s = "http://" + s
    host = (urlparse(s).hostname or "").removeprefix("www.")
    return host if "." in host else None


def has_mx(domain: str) -> bool:
    try:
        import dns.resolver

        return bool(dns.resolver.resolve(domain, "MX", lifetime=4))
    except Exception:
        return False


def split_name(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "", ""
    return parts[0], parts[-1] if len(parts) > 1 else ""


def _ascii(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def pattern_guesses(first: str, last: str, domain: str) -> list[str]:
    f, l = _ascii(first), _ascii(last)
    if not f:
        return []
    guesses = [f"{f}@{domain}"]
    if l:
        guesses += [f"{f}.{l}@{domain}", f"{f[0]}{l}@{domain}", f"{f}{l}@{domain}"]
    return guesses


def email_matches_name(email: str, name: str) -> bool:
    """asha@, ashara@, asha.rao@ (dots are stripped), arao@, raoasha@ all match "Asha Rao"."""
    local = _ascii(email.split("@")[0])
    first, last = (_ascii(x) for x in split_name(name))
    if not first:
        return False
    options = {first}
    if last:
        options |= {first + last, first[0] + last, last + first}
    return local in options


def _text(page_html: str) -> str:
    page_html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", page_html)
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", page_html))).strip()


def linkedin_search_url(name: str, company: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(f'{name} {company}')}"


# ---------------------------------------------------------------- 1. names
def yc_founders(company_name: str, yc_url: str = "", domain: str | None = None) -> list[dict]:
    """Founder names from the public YC company page.

    With a known YC link, use it. Otherwise the page address is guessed from the company name,
    which can belong to a different company with the same name: then the YC page's website must
    match the startup's domain, or we'd draft emails to the wrong founders."""
    guessed = "ycombinator.com/companies/" not in (yc_url or "")
    url = yc_url if not guessed else \
        f"https://www.ycombinator.com/companies/{re.sub(r'[^a-z0-9]+', '-', company_name.lower()).strip('-')}"
    r = _get(url)
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
        if not domain or not site or normalize_domain(site) != domain:
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


def crawl_site(domain: str) -> dict:
    """Fetch a few pages of the startup's site; pull emails, GitHub org, ATS boards and team text."""
    out = {"emails": set(), "github": None, "ats": {}, "team_text": ""}
    base = f"https://{domain}"
    for path in SITE_PATHS:
        r = _get(urljoin(base, path))
        if r is None:
            continue
        page = r.text[:400_000]
        for e in EMAIL_RE.findall(html_lib.unescape(page)):
            e = e.lower().rstrip(".")
            if e.endswith("@" + domain) and not re.search(r"\.(png|jpg|svg|webp)$", e):
                out["emails"].add(e)
        if not out["github"]:
            orgs = [o for o in GITHUB_ORG_RE.findall(page) if o.lower() not in GITHUB_SKIP]
            out["github"] = orgs[0] if orgs else None
        for ats, rx in ATS_RES.items():
            m = rx.search(page)
            if m and ats not in out["ats"]:
                out["ats"][ats] = m.group(1)
        if path in ("/about", "/team", "/company", "/about-us", "") and len(out["team_text"]) < 6000:
            out["team_text"] += " " + _text(page)[:3000]
    out["emails"] = sorted(out["emails"])
    return out


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


# ---------------------------------------------------------------- 2. GitHub
def github_emails(org: str, domain: str, max_repos: int = 4) -> dict[str, str]:
    """{email: author name} for company-domain committers in the org's recently pushed public repos."""
    headers = {"Accept": "application/vnd.github+json"}
    if get_settings().github_token:
        headers["Authorization"] = f"Bearer {get_settings().github_token}"
    repos = None
    for kind in ("orgs", "users"):
        r = _get(f"https://api.github.com/{kind}/{org}/repos", params={"sort": "pushed", "per_page": 20}, headers=headers)
        if r is not None:
            repos = r.json()
            break
    found: dict[str, str] = {}
    for repo in [x for x in repos or [] if not x.get("fork")][:max_repos]:
        r = _get(f"https://api.github.com/repos/{repo['full_name']}/commits", params={"per_page": 100}, headers=headers)
        for c in (r.json() if r is not None else []):
            author = (c.get("commit") or {}).get("author") or {}
            email = (author.get("email") or "").lower()
            if email.endswith("@" + domain) and "noreply" not in email:
                found.setdefault(email, author.get("name") or "")
    return found


# ---------------------------------------------------------------- 3/4. Hunter
def hunter_find(first: str, last: str, domain: str) -> dict | None:
    key = get_settings().hunter_api_key
    if not key or not first:
        return None
    r = _get("https://api.hunter.io/v2/email-finder",
             params={"domain": domain, "first_name": first, "last_name": last, "api_key": key})
    data = (r.json().get("data") if r is not None else None) or {}
    if not data.get("email"):
        return None
    status = (data.get("verification") or {}).get("status")
    return {"email": data["email"].lower(), "confidence": "verified" if status == "valid" else "likely", "source": "hunter"}


def verify(email: str) -> str:
    """'valid' | 'invalid' | 'accept_all' | 'unknown' (no key → unknown)."""
    key = get_settings().hunter_api_key
    if not key:
        return "unknown"
    r = _get("https://api.hunter.io/v2/email-verifier", params={"email": email, "api_key": key})
    status = ((r.json().get("data") if r is not None else None) or {}).get("status", "unknown")
    return status if status in ("valid", "invalid", "accept_all") else "unknown"


# ---------------------------------------------------------------- open roles
def open_roles(ats: dict, domain: str, limit: int = 8) -> list[dict]:
    """Public job listings from Ashby / Lever / Greenhouse. Tries the domain name as the board slug if none linked."""
    slug_guess = domain.split(".")[0]
    candidates = [(k, v) for k, v in ats.items()] or [("ashby", slug_guess), ("lever", slug_guess), ("greenhouse", slug_guess)]
    for kind, slug in candidates:
        jobs = []
        if kind == "ashby":
            r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            for j in (r.json().get("jobs", []) if r is not None else []):
                if j.get("isListed", True):
                    jobs.append({"title": j.get("title", ""), "location": j.get("location", ""),
                                 "remote": bool(j.get("isRemote")), "url": j.get("jobUrl") or j.get("applyUrl", ""), "board": "Ashby"})
        elif kind == "lever":
            r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            for j in (r.json() if r is not None else []):
                cats = j.get("categories") or {}
                jobs.append({"title": j.get("text", ""), "location": cats.get("location", ""),
                             "remote": (j.get("workplaceType") == "remote"), "url": j.get("hostedUrl", ""), "board": "Lever"})
        elif kind == "greenhouse":
            r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            for j in (r.json().get("jobs", []) if r is not None else []):
                loc = (j.get("location") or {}).get("name", "")
                jobs.append({"title": j.get("title", ""), "location": loc, "remote": "remote" in loc.lower(),
                             "url": j.get("absolute_url", ""), "board": "Greenhouse"})
        if jobs:
            return jobs[:limit]
    return []


# ---------------------------------------------------------------- the waterfall
def enrich(name: str, domain: str | None, known_founders: list[dict] | None = None, yc_url: str = "") -> dict:
    """Run the full waterfall. Returns {"founders": [...], "contacts": [...], "jobs": [...], "github": str|None}.

    contacts are sorted best first; each is {email, name, title, confidence, source}.
    """
    result = {"founders": [], "contacts": [], "jobs": [], "github": None,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    if not domain:
        return result

    # 1. founder names
    founders: dict[str, dict] = {}
    for f in (known_founders or []):
        if f.get("name"):
            founders.setdefault(f["name"], {"title": "Founder", "source": "news", **f})
    for f in yc_founders(name, yc_url, domain):
        founders.setdefault(f["name"], f).update({k: v for k, v in f.items() if v and k not in founders[f["name"]]})
    site = crawl_site(domain)
    if not founders:
        for f in names_from_site_text(site["team_text"], name):
            founders.setdefault(f["name"], f)
    for f in founders.values():
        f.setdefault("linkedin_search", linkedin_search_url(f["name"], name))
    result["founders"] = list(founders.values())
    result["github"] = site["github"]

    contacts: dict[str, dict] = {}

    def add(email, confidence, source, person=None):
        email = email.lower()
        cur = contacts.get(email)
        if cur and CONFIDENCE_RANK[cur["confidence"]] >= CONFIDENCE_RANK[confidence]:
            return
        contacts[email] = {"email": email, "confidence": confidence, "source": source,
                           "name": (person or {}).get("name", ""), "title": (person or {}).get("title", "")}

    # 2. published company-domain emails (website + GitHub commits)
    published = {e: "" for e in site["emails"]}
    if site["github"]:
        published.update({e: n for e, n in github_emails(site["github"], domain).items() if e not in published})
    for email, author in published.items():
        person = next((f for f in founders.values() if email_matches_name(email, f["name"])), None)
        if person:
            add(email, "public", "website" if email in site["emails"] else "github", person)
        elif email.split("@")[0] in INBOX_NAMES:
            add(email, "inbox", "website")

    lead = next(iter(founders.values()), None)
    have_founder_email = any(c["name"] for c in contacts.values())

    # 3. paid lookup for the lead founder
    if lead and not have_founder_email:
        first, last = split_name(lead["name"])
        hit = hunter_find(first, last, domain)
        if hit:
            add(hit["email"], hit["confidence"], "hunter", lead)
            have_founder_email = True

    # 4. pattern + verify (max 3 verifier calls)
    if lead and not have_founder_email and has_mx(domain):
        first, last = split_name(lead["name"])
        tries = 0
        for guess in pattern_guesses(first, last, domain):
            status = verify(guess) if tries < 3 else "unknown"
            tries += status != "unknown"
            if status == "valid":
                add(guess, "verified", "pattern", lead)
                break
            if status == "accept_all":  # server takes everything; the plain first-name pattern is the best bet
                add(pattern_guesses(first, last, domain)[0], "likely", "pattern", lead)
                break
            if status == "unknown":  # no verifier: keep the most common startup pattern as a guess
                add(guess, "guess", "pattern", lead)
                break

    # 5. shared inbox fallback
    if not any(c["confidence"] == "inbox" for c in contacts.values()) and has_mx(domain):
        add(f"founders@{domain}", "guess", "inbox-guess", {"name": "", "title": "Founders inbox"})

    result["contacts"] = sorted(contacts.values(), key=lambda c: (-CONFIDENCE_RANK[c["confidence"]], not c["name"]))
    result["jobs"] = open_roles(site["ats"], domain)
    return result


def best_contact(contacts: list[dict]) -> dict | None:
    return contacts[0] if contacts else None
