"""Job-board readers: each turns one board's public API into the same shape.

read(board, slug) -> {"name": company name or None, "jobs": [job, ...]}, or None when the board didn't answer
(None is never treated as "no jobs", so an outage can't close real roles). A job is
{"title", "team", "location", "remote", "url", "posted"} with posted as an ISO date or None.
"""
from app.services.jobs import polite


def _job(title, team="", location="", remote=False, url="", posted=None) -> dict:
    return {"title": (title or "").strip()[:255], "team": (team or "").strip()[:255], "location": (location or "").strip()[:255],
            "remote": bool(remote), "url": (url or "").strip()[:1000], "posted": posted or None}


def greenhouse(slug: str) -> dict | None:
    data = polite.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if data is None:
        return None
    meta = polite.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}") or {}
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(_job(j.get("title"), ((j.get("departments") or [{}])[0] or {}).get("name", ""), loc,
                         "remote" in loc.lower(), j.get("absolute_url"), (j.get("updated_at") or "")[:10] or None))
    return {"name": meta.get("name"), "jobs": jobs}


def lever(slug: str) -> dict | None:
    data = polite.get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if not isinstance(data, list):
        return None
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        jobs.append(_job(j.get("text"), cats.get("team", ""), cats.get("location", ""), j.get("workplaceType") == "remote",
                         j.get("hostedUrl"), _epoch_ms(created)))
    return {"name": None, "jobs": jobs}  # Lever doesn't say whose board it is: only trusted when linked from their site


def ashby(slug: str) -> dict | None:
    data = polite.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if data is None:
        return None
    jobs = [_job(j.get("title"), j.get("department") or j.get("team", ""), j.get("location", ""), j.get("isRemote"),
                 j.get("jobUrl") or j.get("applyUrl"), (j.get("publishedAt") or "")[:10] or None)
            for j in data.get("jobs", []) if j.get("isListed", True)]
    return {"name": None, "jobs": jobs}  # like Lever: trusted only when linked


def workable(slug: str) -> dict | None:
    data = polite.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if data is None:
        return None
    jobs = [_job(j.get("title"), j.get("department", ""), ", ".join(x for x in (j.get("city"), j.get("country")) if x),
                 j.get("telecommuting"), j.get("url") or j.get("application_url"), j.get("published_on"))
            for j in data.get("jobs", [])]
    return {"name": data.get("name"), "jobs": jobs}


def smartrecruiters(slug: str) -> dict | None:
    data = polite.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
    if data is None:
        return None
    jobs, name = [], None
    for j in data.get("content", []):
        loc = j.get("location") or {}
        name = name or (j.get("company") or {}).get("name")
        jobs.append(_job(j.get("name"), (j.get("department") or {}).get("label", ""), loc.get("fullLocation") or loc.get("city", ""),
                         loc.get("remote"), f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}", (j.get("releasedDate") or "")[:10] or None))
    return {"name": name, "jobs": jobs}


def recruitee(slug: str) -> dict | None:
    data = polite.get_json(f"https://{slug}.recruitee.com/api/offers/")
    if data is None:
        return None
    offers = data.get("offers", [])
    jobs = [_job(o.get("title"), o.get("department", ""), o.get("location", ""), o.get("remote"),
                 o.get("careers_url") or o.get("url"), (o.get("published_at") or o.get("created_at") or "")[:10] or None)
            for o in offers]
    return {"name": (offers[0].get("company_name") if offers else None), "jobs": jobs}


def breezy(slug: str) -> dict | None:
    data = polite.get_json(f"https://{slug}.breezy.hr/json")
    if not isinstance(data, list):
        return None
    jobs, name = [], None
    for j in data:
        loc = j.get("location") or {}
        name = name or (j.get("company") or {}).get("name")
        jobs.append(_job(j.get("name"), j.get("department", ""), loc.get("name", ""), loc.get("is_remote"),
                         j.get("url"), (j.get("published_date") or "")[:10] or None))
    return {"name": name, "jobs": jobs}


def _epoch_ms(value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()


READERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
           "smartrecruiters": smartrecruiters, "recruitee": recruitee, "breezy": breezy}
NAMED = ("greenhouse", "workable", "smartrecruiters", "recruitee", "breezy")  # boards that say whose they are


def read(board: str, slug: str) -> dict | None:
    return READERS[board](slug)
