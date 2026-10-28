"""Open roles from the startup's public job board (Ashby, Lever or Greenhouse)."""
from __future__ import annotations

from app.services.contacts import fetch


def open_roles(ats: dict, domain: str, limit: int = 8) -> list[dict]:
    """Public job listings from Ashby / Lever / Greenhouse. Tries the domain name as the board slug if none linked."""
    slug_guess = domain.split(".")[0]
    candidates = [(k, v) for k, v in ats.items()] or [("ashby", slug_guess), ("lever", slug_guess), ("greenhouse", slug_guess)]
    for kind, slug in candidates:
        jobs = []
        if kind == "ashby":
            r = fetch._get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            for j in (r.json().get("jobs", []) if r is not None else []):
                if j.get("isListed", True):
                    jobs.append({"title": j.get("title", ""), "location": j.get("location", ""),
                                 "remote": bool(j.get("isRemote")), "url": j.get("jobUrl") or j.get("applyUrl", ""), "board": "Ashby"})
        elif kind == "lever":
            r = fetch._get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            for j in (r.json() if r is not None else []):
                cats = j.get("categories") or {}
                jobs.append({"title": j.get("text", ""), "location": cats.get("location", ""),
                             "remote": (j.get("workplaceType") == "remote"), "url": j.get("hostedUrl", ""), "board": "Lever"})
        elif kind == "greenhouse":
            r = fetch._get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            for j in (r.json().get("jobs", []) if r is not None else []):
                loc = (j.get("location") or {}).get("name", "")
                jobs.append({"title": j.get("title", ""), "location": loc, "remote": "remote" in loc.lower(),
                             "url": j.get("absolute_url", ""), "board": "Greenhouse"})
        if jobs:
            return jobs[:limit]
    return []
