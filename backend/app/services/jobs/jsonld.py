"""Roles a careers page publishes as structured data (schema.org JobPosting, which Google for Jobs reads)."""
import json
import re
from datetime import date

_LD = re.compile(r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""", re.I | re.S)


def _items(node):
    if isinstance(node, list):
        for x in node:
            yield from _items(x)
    elif isinstance(node, dict):
        if "@graph" in node:
            yield from _items(node["@graph"])
        yield node


def _location(post: dict) -> str:
    locs = post.get("jobLocation") or []
    for loc in (locs if isinstance(locs, list) else [locs]):
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
            text = ", ".join(p if isinstance(p, str) else (p or {}).get("name", "") for p in parts if p)
            if text:
                return text
    return ""


def job_postings(html: str, page_url: str) -> list[dict]:
    """Open roles from JobPosting blocks on the page (expired ones, past validThrough, skipped)."""
    out = []
    for block in _LD.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for post in _items(data):
            kind = post.get("@type")
            if kind != "JobPosting" and not (isinstance(kind, list) and "JobPosting" in kind):
                continue
            valid = str(post.get("validThrough") or "")[:10]
            if valid and valid < date.today().isoformat():
                continue
            title = str(post.get("title") or "").strip()
            if not title:
                continue
            remote = str(post.get("jobLocationType") or "").upper() == "TELECOMMUTE"
            out.append({"title": title[:255], "team": "", "location": _location(post)[:255], "remote": remote,
                        "url": str(post.get("url") or f"{page_url}#{title}")[:1000],
                        "posted": str(post.get("datePosted") or "")[:10] or None})
    return out
