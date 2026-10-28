"""The focus (FOCUS_CITIES, FOCUS_STAGES): which startups users are matched with, and which the database keeps."""
import re

from app.config import get_settings


def _csv(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def normalize_stage(stage: str | None) -> str:
    s = re.sub(r"[\s_]+", " ", (stage or "").strip().lower())
    if s in ("pre seed", "preseed", "pre-seed", "angel"):
        return "pre-seed"
    if s in ("seed", "seed round", "seed stage"):
        return "seed"
    return s


def in_focus(location: str | None, stage: str | None, cities: list[str] | None = None,
             stages: list[str] | None = None) -> bool:
    s = get_settings()
    cities = _csv(s.focus_cities) if cities is None else cities
    stages = _csv(s.focus_stages) if stages is None else stages
    if cities and not any(c in (location or "").lower() for c in cities):
        return False
    if stages and normalize_stage(stage) not in stages:
        return False
    return True


INDIA_PLACES = ("india", "bengaluru", "bangalore", "mumbai", "delhi", "gurugram", "gurgaon", "noida", "pune", "hyderabad",
                "chennai", "kolkata", "ahmedabad", "jaipur", "kochi", "chandigarh", "indore", "coimbatore", "mysuru", "mysore",
                "bhubaneswar", "lucknow", "goa", "thiruvananthapuram", "trivandrum", "surat", "nagpur", "vadodara")


def keep_in_database(row: dict) -> bool:
    """The database keeps every Indian startup (any city, any stage); users are matched only with the focus.
    Indian news rarely names a city in the headline, so a news startup with no known city stays too."""
    loc = (row.get("location") or "").lower()
    if any(p in loc for p in INDIA_PLACES):
        return True
    return row.get("source") in ("news", "startup_map") and not loc


def focus_label() -> str:
    """Plain words for the current focus, e.g. "pre-seed and seed startups in Bengaluru"."""
    s = get_settings()
    stages, cities = _csv(s.focus_stages), _csv(s.focus_cities)
    what = (" and ".join(stages) + " startups") if stages else "startups"
    # bengaluru/bangalore are one city; show the first spelling of each
    where = cities[0].title() if cities else ""
    return f"{what} in {where}" if where else what
