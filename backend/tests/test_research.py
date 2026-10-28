"""Research agent: explores the startup's own site, briefs from what it read, caches per startup."""
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Startup, now  # noqa: E402
from app.services import contacts, research  # noqa: E402

HOME = """<html><body><h1>Acme builds inference infra for Indian languages.</h1>
<a href="/blog/launch-v2">We launched Acme v2</a> <a href="/careers">Careers</a> <a href="/login">Log in</a>
<a href="https://evil.example.com/x">Partner</a> <a href="/logo.png">logo</a></body></html>"""
POST = "<html><body><p>" + "Acme v2 is live: 3x faster Kannada and Hindi inference for banks. " * 10 + "</p></body></html>"


@pytest.fixture
def site(monkeypatch):
    fetched = []

    def handler(request: httpx.Request):
        fetched.append(str(request.url))
        pages = {"https://acme-test.ai": HOME, "https://acme-test.ai/blog/launch-v2": POST,
                 "https://acme-test.ai/careers": "<p>Join us. " * 50 + "</p>"}
        body = pages.get(str(request.url).rstrip("/"))
        return httpx.Response(200, text=body) if body else httpx.Response(404)

    monkeypatch.setattr(contacts.fetch, "_client", httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))
    yield fetched
    contacts.fetch._client = None


@pytest.fixture
def startup():
    init_db()
    with SessionLocal() as db:
        db.query(Startup).filter_by(domain="acme-test.ai").delete()
        s = Startup(name="Acme", domain="acme-test.ai", one_liner="Inference infra", stage="seed", location="Bengaluru")
        db.add(s)
        db.commit()
        yield db, s
        db.query(Startup).filter_by(domain="acme-test.ai").delete()
        db.commit()


def test_links_stay_on_the_startups_own_site():
    links = [u for u, _ in research._links(HOME, "https://acme-test.ai/", "acme-test.ai")]
    assert links == ["https://acme-test.ai/blog/launch-v2", "https://acme-test.ai/careers"]  # no login, assets, other sites


def test_research_reads_picked_pages_and_cites_them(site, startup):
    db, s = startup
    brief = research.research_startup(db, s)
    assert "https://acme-test.ai/blog/launch-v2" in site and not any("evil" in u or "login" in u for u in site)
    assert brief["recent"] and all(r["source"] in brief["sources"] for r in brief["recent"] + brief["hooks"])
    assert s.researched_at is not None
    # cached: a second draft to the same startup doesn't fetch again
    before = len(site)
    assert research.research_startup(db, s) == brief and len(site) == before
    s.researched_at = now() - research.RESEARCH_TTL - timedelta(minutes=1)
    research.research_startup(db, s)
    assert len(site) > before  # stale: refreshed


def test_items_without_a_source_we_read_are_dropped():
    brief = research._clean({"what_they_do": "X", "recent": [{"fact": "Raised $10M", "source": "https://made-up.com"},
                                                            {"fact": "Launched v2", "source": "https://a.ai/blog"}],
                             "hooks": [{"line": "Saw v2", "source": "https://a.ai/blog"}, {"line": "no source"}]},
                            {"https://a.ai", "https://a.ai/blog"})
    assert [r["fact"] for r in brief["recent"]] == ["Launched v2"] and [h["line"] for h in brief["hooks"]] == ["Saw v2"]
    assert research._clean({"what_they_do": "", "recent": [], "hooks": []}, {"https://a.ai"}) is None


def test_unsafe_or_missing_sites_are_skipped(site):
    with SessionLocal() as db:
        for d in ("127.0.0.1", "localhost", "intranet.local", None):
            assert research.research_startup(db, Startup(name="X", domain=d)) is None
    assert site == []  # nothing was fetched


def test_research_goes_into_the_draft_prompt():
    text = research.brief_for_prompt({"what_they_do": "Inference infra", "recent": [{"fact": "Launched v2", "source": "u"}],
                                      "building": ["Kannada ASR"], "hooks": [{"line": "Saw v2 is live", "source": "u"}]})
    assert "Launched v2" in text and "Saw v2 is live" in text and "ONE specific fact" in text
    assert "none found" in research.brief_for_prompt(None)
