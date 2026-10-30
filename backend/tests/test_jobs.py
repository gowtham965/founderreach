"""Open roles: job-board readers, finding boards, careers-page data, the open/close lifecycle, the nightly run."""
import json
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Job, JobRun, Startup, now  # noqa: E402
from app.services import jobs  # noqa: E402
from app.services.jobs import boards, detect, jsonld, polite, refresh  # noqa: E402


class Web:
    """A fake internet: {url: (status, body)}; everything else is a 404. Records what was fetched."""

    def __init__(self):
        self.routes, self.fetched = {}, []

    def __call__(self, request: httpx.Request):
        url = str(request.url).split("?")[0].rstrip("/")
        self.fetched.append(url)
        status, body = self.routes.get(url, (404, "not found"))
        return httpx.Response(status, json=body) if isinstance(body, (dict, list)) else httpx.Response(status, text=body)


@pytest.fixture
def web(monkeypatch):
    w = Web()
    monkeypatch.setattr(polite, "_client", httpx.Client(transport=httpx.MockTransport(w), follow_redirects=True))
    monkeypatch.setattr(polite, "MIN_GAP", 0)
    polite._robots.clear()
    polite._last_hit.clear()
    yield w
    polite._client = None


@pytest.fixture
def startup(monkeypatch):
    init_db()
    monkeypatch.setattr(get_settings(), "focus_cities", "bengaluru,bangalore")
    monkeypatch.setattr(get_settings(), "focus_stages", "pre-seed,seed")
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Startup).filter(Startup.domain.like("%.jobtest")).delete(synchronize_session=False)
        s = Startup(name="Acme Robotics", domain="acme.jobtest", stage="seed", location="HSR Layout, Bengaluru")
        db.add(s)
        db.commit()
        yield db, s
        db.query(Job).delete()
        db.query(Startup).filter(Startup.domain.like("%.jobtest")).delete(synchronize_session=False)
        db.commit()


LEVER = [{"text": "Backend Engineer", "categories": {"team": "Eng", "location": "Bengaluru"}, "workplaceType": "onsite",
          "hostedUrl": "https://jobs.lever.co/acme/1", "createdAt": 1793000000000},
         {"text": "Designer", "categories": {"location": "Remote"}, "workplaceType": "remote", "hostedUrl": "https://jobs.lever.co/acme/2"}]


def test_board_readers_share_one_shape(web):
    web.routes["https://boards-api.greenhouse.io/v1/boards/acme/jobs"] = (200, {"jobs": [
        {"title": "ML Engineer", "absolute_url": "https://gh/1", "location": {"name": "Remote - India"}, "updated_at": "2026-10-20T00:00:00Z"}]})
    web.routes["https://boards-api.greenhouse.io/v1/boards/acme"] = (200, {"name": "Acme Robotics Pvt Ltd"})
    web.routes["https://api.lever.co/v0/postings/acme"] = (200, LEVER)
    web.routes["https://apply.workable.com/api/v1/widget/accounts/acme"] = (200, {"name": "Acme", "jobs": [
        {"title": "SDR", "url": "https://w/1", "city": "Bengaluru", "country": "India", "telecommuting": False, "published_on": "2026-10-01"}]})
    gh, lv, wk = boards.read("greenhouse", "acme"), boards.read("lever", "acme"), boards.read("workable", "acme")
    assert gh["name"] == "Acme Robotics Pvt Ltd" and gh["jobs"][0] == {"title": "ML Engineer", "team": "", "location": "Remote - India",
                                                                         "remote": True, "url": "https://gh/1", "posted": "2026-10-20"}
    assert lv["name"] is None and [j["remote"] for j in lv["jobs"]] == [False, True] and lv["jobs"][0]["posted"]
    assert wk["jobs"][0]["location"] == "Bengaluru, India"
    assert boards.read("ashby", "nobody") is None  # a board that doesn't answer is "unknown", never "no jobs"


def test_board_links_and_careers_pages_stay_on_site(web):
    html = ('<a href="https://jobs.lever.co/acme">Jobs</a> <a href="https://acme.recruitee.com/">x</a>'
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmeco"></iframe>')
    assert detect.boards_in(html) == {"lever": "acme", "recruitee": "acme", "greenhouse": "acmeco"}
    web.routes["https://acme.jobtest"] = (200, '<a href="/team/careers">We are hiring</a> <a href="https://evil.example/jobs">x</a>')
    web.routes["https://acme.jobtest/team/careers"] = (200, "careers here")
    urls = [u for u, _ in detect.careers_pages("acme.jobtest")]
    assert "https://acme.jobtest/team/careers" in urls and not any("evil" in u for u in web.fetched)


def test_robots_txt_is_respected(web):
    web.routes["https://acme.jobtest/robots.txt"] = (200, "User-agent: *\nDisallow: /careers")
    web.routes["https://acme.jobtest/careers"] = (200, "secret")
    assert polite.get("https://acme.jobtest/careers", check_robots=True) is None
    assert "https://acme.jobtest/careers" not in web.fetched


def test_job_posting_data_on_careers_pages():
    page = '<script type="application/ld+json">' + json.dumps({"@graph": [
        {"@type": "JobPosting", "title": "Founding Engineer", "url": "https://acme.jobtest/j/1", "datePosted": "2026-10-10",
         "jobLocation": {"address": {"addressLocality": "Bengaluru", "addressCountry": "IN"}}},
        {"@type": "JobPosting", "title": "Old role", "validThrough": "2020-01-01"},
        {"@type": "JobPosting", "title": "Remote PM", "jobLocationType": "TELECOMMUTE"},
        {"@type": "Organization", "name": "Acme"}]}) + "</script>"
    roles = jsonld.job_postings(page, "https://acme.jobtest/careers")
    assert [r["title"] for r in roles] == ["Founding Engineer", "Remote PM"]
    assert roles[0]["location"] == "Bengaluru, IN" and roles[1]["remote"] and roles[1]["url"] == "https://acme.jobtest/careers#Remote PM"


def test_same_company_and_plausible_guesses():
    assert refresh.same_company("Razorpay Software Private Limited", "Razorpay")
    assert refresh.same_company("ACME", "Acme Technologies") and not refresh.same_company("Acmeville Foods", "Zeta")
    india = {"name": "Infinity", "jobs": [{"title": "x", "remote": False, "location": "Bengaluru, India"}]}
    elsewhere = {"name": "Infinity", "jobs": [{"title": "x", "remote": False, "location": "Austin, TX"}]}
    assert refresh.plausible(india, "Infinity") and not refresh.plausible(elsewhere, "Infinity")
    assert not refresh.plausible({"name": "Infinity", "jobs": []}, "Infinity")  # an empty board proves nothing


def test_roles_open_stay_and_close_after_two_misses(web, startup):
    db, s = startup
    web.routes["https://acme.jobtest"] = (200, '<a href="https://jobs.lever.co/acme">Careers</a>')
    web.routes["https://api.lever.co/v0/postings/acme"] = (200, LEVER)
    assert jobs.refresh_startup(db, s) == {"new": 2, "closed": 0, "open": 2}
    assert s.job_boards["boards"] == {"lever": "acme"} and s.jobs[0]["title"] in ("Backend Engineer", "Designer")

    web.routes["https://api.lever.co/v0/postings/acme"] = (200, LEVER[:1])  # the designer role is gone
    assert jobs.refresh_startup(db, s)["open"] == 2  # one miss: still open
    web.routes["https://api.lever.co/v0/postings/acme"] = (500, "down")
    assert jobs.refresh_startup(db, s)["closed"] == 0  # the board didn't answer: misses don't count
    web.routes["https://api.lever.co/v0/postings/acme"] = (200, LEVER[:1])
    assert jobs.refresh_startup(db, s) == {"new": 0, "closed": 1, "open": 1}
    assert [j["title"] for j in s.jobs] == ["Backend Engineer"]
    web.routes["https://api.lever.co/v0/postings/acme"] = (200, LEVER)
    assert jobs.refresh_startup(db, s)["open"] == 2  # it came back: reopened
    designer = db.query(Job).filter_by(title="Designer").one()
    assert designer.closed_at is None and designer.missed == 0


def test_careers_page_copy_of_a_board_role_is_dropped(web, startup):
    db, s = startup
    ld = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "Backend Engineer"}) + "</script>"
    web.routes["https://acme.jobtest"] = (200, '<a href="https://jobs.lever.co/acme">Careers</a>' + ld)
    web.routes["https://api.lever.co/v0/postings/acme"] = (200, LEVER)
    jobs.refresh_startup(db, s)
    assert sorted(j["title"] for j in s.jobs) == ["Backend Engineer", "Designer"]


def test_guessed_board_needs_name_and_india_role(web, startup):
    db, s = startup
    web.routes["https://acme.jobtest"] = (200, "no careers links")
    web.routes["https://apply.workable.com/api/v1/widget/accounts/acme"] = (200, {"name": "Acme Robotics", "jobs": [
        {"title": "Sales", "url": "https://w/9", "city": "Austin", "country": "USA"}]})
    jobs.refresh_startup(db, s)
    assert s.job_boards["boards"] == {} and s.job_boards["guessed_at"]  # same name, but nothing in India: not trusted
    web.fetched.clear()
    jobs.refresh_startup(db, s)
    assert not any("workable" in u for u in web.fetched)  # not guessed again until next week


def test_nightly_order_and_budget(web, startup, monkeypatch):
    db, s = startup
    other = Startup(name="Far Away", domain="far.jobtest", stage="series b", location="Mumbai, India")
    hidden = Startup(name="Hidden Co", domain="hid.jobtest", stage="seed", location="Bengaluru", hidden=True)
    recent = Startup(name="Checked", domain="chk.jobtest", stage="series a", location="Pune", jobs_checked_at=now() - timedelta(days=1))
    db.add_all([other, hidden, recent])
    db.commit()
    ids = refresh.due(db)
    assert s.id in ids and other.id in ids and hidden.id not in ids and recent.id not in ids
    assert ids.index(s.id) < ids.index(other.id)  # the focus goes first
    stats = jobs.run(db, budget_minutes=0)
    assert stats["checked"] == 0 and stats["left"] == len(ids)


def test_scheduler_records_the_run(web, startup, monkeypatch):
    from app import scheduler
    monkeypatch.setattr(refresh, "due", lambda db, weekly_cap=150: [startup[1].id])
    with SessionLocal() as db:
        db.query(JobRun).filter_by(name="jobs").delete()
        db.commit()
    scheduler.job_jobs()
    with SessionLocal() as db:
        run = db.query(JobRun).filter_by(name="jobs").one()
    assert run.ok and run.detail["checked"] == 1


def test_contact_lookup_keeps_fresher_roles(startup, monkeypatch):
    db, s = startup
    from app.services import outreach
    s.jobs, s.jobs_checked_at = [{"title": "From the nightly refresh"}], now()
    db.commit()
    monkeypatch.setattr(outreach.enrich.contacts_svc, "enrich", lambda *a, **k: {"founders": [], "contacts": [], "jobs": [{"title": "old"}], "github": None})
    outreach.enrich_startup(db, s, force=True)
    assert s.jobs == [{"title": "From the nightly refresh"}]


# ---------------------------------------------------------------- phase 2: roles in matching, drafts, alerts, ops
def _user(db, email, roles=("AI engineer",), skills=("python",)):
    from app.models import Profile, User
    db.query(User).filter_by(email=email).delete()
    u = User(email=email, password_hash="", email_verified_at=now(), last_seen_at=now(),
             profile=Profile(summary="Builds things", skills=list(skills), target_roles=list(roles), preferences={}, links={}, highlights=[]))
    db.add(u)
    db.commit()
    return u


def test_new_role_alerts_fit_the_user(startup):
    from app.models import Match
    from app.services.jobs import fit
    db, s = startup
    other = Startup(name="Beta Labs", domain="beta.jobtest", stage="seed", location="Koramangala, Bengaluru")
    far = Startup(name="Gamma", domain="gamma.jobtest", stage="series c", location="Mumbai, India")
    db.add_all([other, far])
    db.commit()
    add = lambda st, title, ago=1: db.add(Job(startup_id=st.id, title=title, url=f"https://x/{st.id}/{title}", board="lever",
                                               first_seen_at=now() - timedelta(hours=ago), last_seen_at=now()))
    add(s, "AI Engineer"); add(s, "Product Designer"); add(other, "Senior Python Developer")
    add(other, "Sales Engineer"); add(far, "AI Engineer"); add(s, "ML Platform (AI) Lead", ago=48)
    db.commit()
    u = _user(db, "roles-fit@gmail.com")
    titles = [j.title for j, _ in fit.new_roles_for(db, u)]
    assert set(titles) == {"AI Engineer", "Senior Python Developer"}  # not design, not "engineer" alone, not outside focus, not old
    db.add(Match(user_id=u.id, startup_id=other.id, status="skipped", score=60))
    db.commit()
    assert [j.title for j, _ in fit.new_roles_for(db, u)] == ["AI Engineer"]  # skipped startups stay quiet
    msg = fit.message(fit.new_roles_for(db, u))
    assert msg.startswith("🆕 New roles that fit you") and "AI Engineer at Acme Robotics" in msg
    db.query(Match).filter_by(user_id=u.id).delete()
    db.query(Startup).filter(Startup.id.in_([other.id, far.id])).delete(synchronize_session=False)
    db.commit()


def test_hiring_startups_rank_higher_in_the_prefilter(startup):
    from app.services import matching
    db, s = startup
    quiet = Startup(name="Quiet", domain="quiet.jobtest", stage="seed", location="Bengaluru", one_liner="payments", signal="", tags=[])
    u = _user(db, "roles-rank@gmail.com", roles=("backend engineer",), skills=("python", "payments"))
    s.one_liner, s.signal, s.tags, s.jobs = "payments", "", [], [{"title": "Python Backend Engineer"}]
    assert matching._prefilter(u, [quiet, s])[0] is s
    db.rollback()  # leave the shared startup as it was


def test_drafts_see_when_roles_were_posted(startup):
    from app.services.outreach import enrich
    db, s = startup
    s.jobs = [{"title": "Backend Engineer", "location": "Bengaluru", "posted": "2026-10-20"}]
    assert enrich._roles_line(s) == "Open roles: Backend Engineer (Bengaluru, posted 2026-10-20)"
    db.rollback()


def test_ops_report_counts_roles(startup):
    from app.agents import ops
    db, s = startup
    db.add(Job(startup_id=s.id, title="X", url="https://x/ops", board="lever", first_seen_at=now(), last_seen_at=now()))
    db.commit()
    data = ops.collect(db)
    assert data["roles"]["open"] >= 1 and data["roles"]["new"] >= 1 and "Open roles:" in ops.render(data, [], "")


# ---------------------------------------------------------------- the chat assistant and open roles
def test_assistant_sees_real_roles_and_can_check_now(web, startup):
    from app.agents import chat as agent
    from app.models import Match
    db, s = startup
    u = _user(db, "roles-chat@gmail.com")
    m = Match(user_id=u.id, startup_id=s.id, score=80, reason="fits")
    db.add(m)
    db.commit()
    listed = agent._run_tool(db, u, "list_matches", {}, "which of these are hiring?", [])["matches"][0]
    assert listed["open_roles"] == [] and listed["roles_checked"] == "not checked yet"  # no guessing from nothing
    web.routes["https://acme.jobtest"] = (200, '<a href="https://jobs.lever.co/acme">Careers</a>')
    web.routes["https://api.lever.co/v0/postings/acme"] = (200, LEVER)
    cards = []
    checked = agent._run_tool(db, u, "check_open_roles", {"match_id": m.id}, "is Acme hiring?", cards)
    assert checked["open_roles_total"] == 2 and any("Backend Engineer" in r for r in checked["open_roles"])
    assert checked["roles_checked"] != "not checked yet" and cards[0]["item"]["open_roles"] == 2
    other = _user(db, "roles-chat-other@gmail.com")
    assert agent._run_tool(db, other, "check_open_roles", {"match_id": m.id}, "x", [])["ok"] is False  # not their match
    db.query(Match).filter_by(id=m.id).delete()
    db.commit()


def test_roles_refresh_runs_one_at_a_time(monkeypatch):
    from app import scheduler
    calls = []
    monkeypatch.setattr(jobs, "run", lambda db: calls.append(1) or {"checked": 0, "failed": 0})
    assert scheduler._jobs_running.acquire(blocking=False)
    try:
        scheduler.job_jobs()  # a second click while one is running does nothing
    finally:
        scheduler._jobs_running.release()
    assert calls == []
    scheduler.job_jobs()
    assert calls == [1]
