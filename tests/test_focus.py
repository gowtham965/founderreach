"""Bangalore + pre-seed/seed focus: what gets into the pool and into matches."""
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402

from app.services import discovery  # noqa: E402

BLR = ["bengaluru", "bangalore"]
SEED = ["pre-seed", "seed"]


@pytest.mark.parametrize("location,stage,ok", [
    ("Bengaluru, KA, India", "seed", True),
    ("Bangalore", "Pre-Seed", True),
    ("bengaluru", "angel", True),           # angel rounds count as pre-seed
    ("Mumbai, India", "seed", False),
    ("Bengaluru", "series a", False),
    ("Bengaluru", "unknown", False),        # unknown stage: left out, not guessed
    (None, "seed", False),                  # unknown city: left out, not guessed
    ("", "seed", False),
])
def test_in_focus(location, stage, ok):
    assert discovery.in_focus(location, stage, BLR, SEED) is ok


def test_empty_focus_allows_everything():
    assert discovery.in_focus(None, "", [], [])


def test_yc_only_recent_early_batches_count_as_seed():
    year = datetime.now(timezone.utc).year
    assert discovery._yc_stage({"stage": "Early", "batch": f"Winter {year}"}) == "seed"
    assert discovery._yc_stage({"stage": "Early", "batch": "Winter 2017"}) == "early"
    assert discovery._yc_stage({"stage": "Growth", "batch": f"Winter {year}"}) == "growth"


def test_run_discovery_keeps_only_focus(monkeypatch):
    from app.config import get_settings
    from app.db import SessionLocal, init_db
    from app.models import Startup

    init_db()
    monkeypatch.setattr(get_settings(), "focus_cities", "bengaluru,bangalore")
    monkeypatch.setattr(get_settings(), "focus_stages", "pre-seed,seed")
    rows = [
        {"name": "FocusIn BLR", "domain": "focusin-blr.in", "location": "Bengaluru, India", "stage": "seed"},
        {"name": "FocusOut Mumbai", "domain": "focusout-mum.in", "location": "Mumbai, India", "stage": "seed"},
        {"name": "FocusOut SeriesB", "domain": "focusout-b.in", "location": "Bengaluru", "stage": "series b"},
    ]
    monkeypatch.setattr(discovery, "SOURCES", [lambda: rows])
    with SessionLocal() as db:
        assert discovery.run_discovery(db) == 1
        names = {s.name for s in db.query(Startup).filter(Startup.name.like("Focus%"))}
    assert names == {"FocusIn BLR"}


def test_focus_label():
    from app.config import get_settings
    s = get_settings()
    old = (s.focus_cities, s.focus_stages)
    try:
        s.focus_cities, s.focus_stages = "bengaluru,bangalore", "pre-seed,seed"
        assert discovery.focus_label() == "pre-seed and seed startups in Bengaluru"
        s.focus_cities, s.focus_stages = "", ""
        assert discovery.focus_label() == "startups"
    finally:
        s.focus_cities, s.focus_stages = old


def _map_page(companies):
    """A tiny stand-in for the Startup Map homepage: directory JSON inside Next.js push() strings."""
    import json
    payload = json.dumps({"companies": companies})
    half = len(payload) // 2  # split across two pushes, like the real page
    return "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>" for part in (payload[:half], payload[half:]))


def test_startup_map_keeps_recent_seed_startups(monkeypatch):
    import httpx

    from app.db import init_db

    init_db()
    year = datetime.now(timezone.utc).year
    companies = [
        {"name": "MapSeed", "slug": "mapseed", "kind": "startup", "tagline": "AI for clinics", "stage": "Seed",
         "sector": "Healthtech", "tags": ["AI"], "area": "HSR Layout", "website": "https://mapseed.in",
         "founders": "Asha Rao, Ravi K", "founded_year": year - 1},
        {"name": "MapPre", "slug": "mappre", "kind": "startup", "tagline": "x", "stage": "Pre-seed",
         "area": "Bengaluru", "website": "https://mappre.in", "founders": "$undefined", "founded_year": "$undefined"},
        {"name": "MapOld", "slug": "mapold", "kind": "startup", "stage": "Seed", "website": "https://mapold.in", "founded_year": 2012},
        {"name": "MapA", "slug": "mapa", "kind": "startup", "stage": "Series A", "website": "https://mapa.in", "founded_year": year},
        {"name": "MapVC", "slug": "mapvc", "kind": "vc", "stage": "Seed", "website": "https://mapvc.in", "founded_year": year},
    ]
    page = _map_page(companies)
    monkeypatch.setattr(discovery.httpx, "get", lambda *a, **k: httpx.Response(200, text=page, request=httpx.Request("GET", a[0])))
    rows = discovery.fetch_startup_map()
    assert [r["name"] for r in rows] == ["MapSeed", "MapPre"]
    seed = rows[0]
    assert seed["location"] == "HSR Layout, Bengaluru" and seed["stage"] == "seed" and seed["domain"] == "mapseed.in"
    assert [f["name"] for f in seed["founders"]] == ["Asha Rao", "Ravi K"]
    assert seed["source_url"].endswith("/company/mapseed") and "Healthtech" in seed["tags"]
    assert rows[1]["founders"] == [] and rows[1]["location"] == "Bengaluru"
    assert all(discovery.in_focus(r["location"], r["stage"], BLR, SEED) for r in rows)


def test_startup_map_reads_at_most_weekly(monkeypatch):
    from app.db import SessionLocal, init_db
    from app.models import Startup

    init_db()
    with SessionLocal() as db:
        db.add(Startup(name="WeeklyMarker", domain="weeklymarker.in", source="startup_map"))
        db.commit()
    monkeypatch.setattr(discovery.httpx, "get", lambda *a, **k: pytest.fail("should not fetch twice in a week"))
    assert discovery.fetch_startup_map() == []


def test_startup_map_can_be_switched_off(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "startup_map_enabled", False)
    monkeypatch.setattr(discovery.httpx, "get", lambda *a, **k: pytest.fail("disabled source fetched"))
    assert discovery.fetch_startup_map() == []
