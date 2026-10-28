"""Contact waterfall against a fake internet (httpx MockTransport)."""
import json
import os

os.environ.setdefault("LLM_PROVIDER", "fake")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import contacts as c  # noqa: E402

def yc_page(website: str, founders=("Asha Rao", "Dev Kumar")) -> str:
    """A YC company page as the site serves it: props as JSON in data-page, including the website."""
    import html
    props = {"props": {"company": {"website": website, "founders": [{"full_name": n, "title": "Founder"} for n in founders]}}}
    return f'<div data-page="{html.escape(json.dumps(props))}"></div>'
HOME = ('<footer>Questions? <a href="mailto:hello@vectorloop.ai">hello@vectorloop.ai</a> '
        '<a href="https://github.com/vectorloop">GitHub</a> <a href="https://jobs.ashbyhq.com/vectorloop">Careers</a></footer>')
REPOS = [{"full_name": "vectorloop/sdk", "fork": False}, {"full_name": "vectorloop/fork", "fork": True}]
COMMITS = [
    {"commit": {"author": {"name": "Asha Rao", "email": "asha@vectorloop.ai"}}},
    {"commit": {"author": {"name": "Asha Rao", "email": "asha.personal@gmail.com"}}},  # personal: ignored
    {"commit": {"author": {"name": "bot", "email": "123+bot@users.noreply.github.com"}}},
]
ASHBY = {"jobs": [{"title": "Founding AI Engineer", "location": "Bengaluru", "isRemote": True, "isListed": True,
                   "jobUrl": "https://jobs.ashbyhq.com/vectorloop/1"}]}


def internet(routes: dict, calls: list):
    def handler(req: httpx.Request):
        calls.append(str(req.url))
        for prefix, payload in routes.items():
            if str(req.url).startswith(prefix):
                if isinstance(payload, (dict, list)):
                    return httpx.Response(200, json=payload)
                return httpx.Response(200, text=payload)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def net(monkeypatch):
    calls: list[str] = []

    def install(routes):
        monkeypatch.setattr(c.fetch, "_client", internet(routes, calls))
        return calls

    monkeypatch.setattr(c.emails, "has_mx", lambda d: True)
    return install


def test_full_waterfall_prefers_published_github_email(net):
    calls = net({
        "https://www.ycombinator.com/companies/vectorloop": yc_page("https://vectorloop.ai"),
        "https://vectorloop.ai/": HOME,
        "https://api.github.com/orgs/vectorloop/repos": REPOS,
        "https://api.github.com/repos/vectorloop/sdk/commits": COMMITS,
        "https://api.ashbyhq.com/posting-api/job-board/vectorloop": ASHBY,
    })
    r = c.enrich("VectorLoop", "vectorloop.ai")
    assert [f["name"] for f in r["founders"]] == ["Asha Rao", "Dev Kumar"]
    best = r["contacts"][0]
    assert best == {"email": "asha@vectorloop.ai", "confidence": "public", "source": "github", "name": "Asha Rao", "title": "Founder"}
    assert all("gmail.com" not in x["email"] for x in r["contacts"])
    assert any(x["email"] == "hello@vectorloop.ai" and x["confidence"] == "inbox" for x in r["contacts"])
    assert r["jobs"][0]["title"] == "Founding AI Engineer" and r["jobs"][0]["remote"]
    assert not any("vectorloop/fork" in u for u in calls)  # forks skipped
    assert not any("linkedin.com" in u for u in calls)  # never fetches LinkedIn
    assert "linkedin.com/search" in r["founders"][0]["linkedin_search"]


def test_pattern_guess_without_keys(net):
    net({"https://www.ycombinator.com/companies/ledgerly": yc_page("https://www.ledgerly.io/")})
    r = c.enrich("Ledgerly", "ledgerly.io")
    assert r["contacts"][0]["email"] == "asha@ledgerly.io" and r["contacts"][0]["confidence"] == "guess"
    assert r["contacts"][-1]["email"] == "founders@ledgerly.io"


def test_verifier_walks_patterns_until_valid(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "hunter_api_key", "k")
    statuses = {"asha@ledgerly.io": "invalid", "asha.rao@ledgerly.io": "valid"}

    def handler_routes():
        return {"https://www.ycombinator.com/companies/ledgerly": yc_page("https://www.ledgerly.io/")}

    calls = net(handler_routes())
    monkeypatch.setattr(c.emails, "hunter_find", lambda *a: None)
    monkeypatch.setattr(c.emails, "verify", lambda e: statuses.get(e, "invalid"))
    r = c.enrich("Ledgerly", "ledgerly.io")
    assert r["contacts"][0] == {"email": "asha.rao@ledgerly.io", "confidence": "verified", "source": "pattern",
                                "name": "Asha Rao", "title": "Founder"}


def test_accept_all_domain_is_marked_likely(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "hunter_api_key", "k")
    net({"https://www.ycombinator.com/companies/ledgerly": yc_page("https://www.ledgerly.io/")})
    monkeypatch.setattr(c.emails, "hunter_find", lambda *a: None)
    monkeypatch.setattr(c.emails, "verify", lambda e: "accept_all")
    assert c.enrich("Ledgerly", "ledgerly.io")["contacts"][0]["confidence"] == "likely"


def test_hunter_finder_used_once_before_patterns(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "hunter_api_key", "k")
    net({
        "https://www.ycombinator.com/companies/ledgerly": yc_page("https://www.ledgerly.io/"),
        "https://api.hunter.io/v2/email-finder": {"data": {"email": "asha@ledgerly.io", "verification": {"status": "valid"}}},
    })
    called = []
    monkeypatch.setattr(c.emails, "verify", lambda e: called.append(e) or "valid")
    r = c.enrich("Ledgerly", "ledgerly.io")
    assert r["contacts"][0]["source"] == "hunter" and r["contacts"][0]["confidence"] == "verified"
    assert called == []  # no extra verifier spend


def test_founders_from_website_when_no_yc(net):
    net({"https://acme.dev/about": "<h2>Team</h2><p>Priya Shah, co-founder and CEO. Rahul Iyer, founder and CTO.</p>" * 5})
    r = c.enrich("Acme", "acme.dev")
    assert {f["name"] for f in r["founders"]} == {"Priya Shah", "Rahul Iyer"}


def test_lever_and_greenhouse_boards(net):
    net({"https://api.lever.co/v0/postings/acme": [{"text": "Backend Engineer", "hostedUrl": "https://jobs.lever.co/acme/1",
                                                     "categories": {"location": "Remote"}, "workplaceType": "remote"}]})
    assert c.open_roles({}, "acme.dev")[0]["board"] == "Lever"
    net({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"title": "ML Engineer", "absolute_url": "u",
                                                                            "location": {"name": "Remote, India"}}]}})
    assert c.open_roles({}, "acme.dev")[0]["remote"] is True


def test_yc_data_page_json(net):
    page = {"props": {"company": {"website": "https://resend.com", "founders": [
        {"full_name": "Zeno Rocha", "title": "CEO", "linkedin_url": "https://linkedin.com/in/zeno"}]}}}
    import html
    net({"https://www.ycombinator.com/companies/resend": f'<div data-page="{html.escape(json.dumps(page))}"></div>'})
    f = c.yc_founders("Resend", domain="resend.com")[0]
    assert f["name"] == "Zeno Rocha" and f["linkedin"] == "https://linkedin.com/in/zeno"


def test_same_named_yc_company_is_not_trusted(net):
    """A YC page guessed from the name belongs to someone else: their founders must not be used."""
    net({"https://www.ycombinator.com/companies/infinity": yc_page("https://infinity-other.com", ["Wrong Person"])})
    assert c.yc_founders("Infinity", domain="infinity.in") == []
    assert c.yc_founders("Infinity") == []  # no domain to check against: don't trust a guessed page
    r = c.enrich("Infinity", "infinity.in")
    assert all(f["name"] != "Wrong Person" for f in r["founders"])


def test_known_yc_link_is_trusted(net):
    net({"https://www.ycombinator.com/companies/infinity-2": yc_page("https://infinity.in", ["Real Founder"])})
    assert c.yc_founders("Infinity", "https://www.ycombinator.com/companies/infinity-2")[0]["name"] == "Real Founder"


def test_email_matches_name():
    assert c.email_matches_name("asha.rao@x.ai", "Asha Rao")
    assert c.email_matches_name("arao@x.ai", "Asha Rao")
    assert not c.email_matches_name("support@x.ai", "Asha Rao")
