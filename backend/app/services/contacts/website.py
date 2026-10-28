"""The startup's own website (emails, GitHub org, job boards, team text) and its public GitHub commits."""
from __future__ import annotations

import html as html_lib
import re
from urllib.parse import urljoin

from app.config import get_settings
from app.services.contacts import fetch

SITE_PATHS = ["", "/about", "/team", "/company", "/about-us", "/careers", "/jobs", "/contact"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GITHUB_ORG_RE = re.compile(r"github\.com/([A-Za-z0-9-]{1,39})(?=[/\"'?#\s]|$)")
ATS_RES = {
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)"),
    "greenhouse": re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9._-]+)"),
}
GITHUB_SKIP = {"features", "pricing", "about", "login", "sponsors", "orgs", "topics", "collections", "marketplace", "apps"}


def crawl_site(domain: str) -> dict:
    """Fetch a few pages of the startup's site; pull emails, GitHub org, ATS boards and team text."""
    out = {"emails": set(), "github": None, "ats": {}, "team_text": ""}
    base = f"https://{domain}"
    for path in SITE_PATHS:
        r = fetch._get(urljoin(base, path))
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
            out["team_text"] += " " + fetch._text(page)[:3000]
    out["emails"] = sorted(out["emails"])
    return out


def github_emails(org: str, domain: str, max_repos: int = 4) -> dict[str, str]:
    """{email: author name} for company-domain committers in the org's recently pushed public repos."""
    headers = {"Accept": "application/vnd.github+json"}
    if get_settings().github_token:
        headers["Authorization"] = f"Bearer {get_settings().github_token}"
    repos = None
    for kind in ("orgs", "users"):
        r = fetch._get(f"https://api.github.com/{kind}/{org}/repos", params={"sort": "pushed", "per_page": 20}, headers=headers)
        if r is not None:
            repos = r.json()
            break
    found: dict[str, str] = {}
    for repo in [x for x in repos or [] if not x.get("fork")][:max_repos]:
        r = fetch._get(f"https://api.github.com/repos/{repo['full_name']}/commits", params={"per_page": 100}, headers=headers)
        for c in (r.json() if r is not None else []):
            author = (c.get("commit") or {}).get("author") or {}
            email = (author.get("email") or "").lower()
            if email.endswith("@" + domain) and "noreply" not in email:
                found.setdefault(email, author.get("name") or "")
    return found
