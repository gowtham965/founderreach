"""Y Combinator companies currently hiring (the public yc-oss JSON mirror)."""
import logging
import re
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.services.contacts import normalize_domain

log = logging.getLogger(__name__)


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
