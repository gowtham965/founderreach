"""Admin → Startups: browse, edit (kept through refreshes), add, hide (never reaches users), CSV export/import."""
import csv
import io
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Startup, User, now  # noqa: E402
from app.services import discovery, matching  # noqa: E402

PREFIX = "Zst"  # every startup these tests make starts with this, so cleanup is easy


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def admin(client, monkeypatch):
    main._FAILS.clear()
    main._SIGNUPS.clear()
    monkeypatch.setattr(get_settings(), "focus_cities", "bengaluru,bangalore")
    monkeypatch.setattr(get_settings(), "focus_stages", "pre-seed,seed")
    hs = {}
    for e in ("st-admin@gmail.com", "st-user@gmail.com"):
        r = client.post("/api/auth/signup", json={"email": e, "password": "password123"})
        if r.status_code == 200:
            hs[e] = {"Authorization": f"Bearer {r.json()['token']}"}
        else:
            hs[e] = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'email': e, 'password': 'password123'}).json()['token']}"}
    with SessionLocal() as db:
        for e in hs:
            db.query(User).filter_by(email=e).one().email_verified_at = now()
        db.query(Startup).filter(Startup.name.like(f"{PREFIX}%")).delete(synchronize_session=False)
        db.add_all([
            Startup(name=f"{PREFIX} Alpha", domain="zst-alpha.in", stage="seed", location="HSR Layout, Bengaluru", source="news",
                    one_liner="AI for kirana stores"),
            Startup(name=f"{PREFIX} Beta", domain="zst-beta.in", stage="series a", location="Mumbai, India", source="yc"),
        ])
        db.commit()
    monkeypatch.setattr(get_settings(), "admin_emails", "st-admin@gmail.com")
    yield hs["st-admin@gmail.com"], hs["st-user@gmail.com"]
    with SessionLocal() as db:
        db.query(Startup).filter(Startup.name.like(f"{PREFIX}%")).delete(synchronize_session=False)
        db.commit()


def find(client, h, **params):
    return client.get("/api/admin/startups", headers=h, params={"q": PREFIX, **params}).json()


def test_admins_only(client, admin):
    _, user = admin
    for call in (lambda: client.get("/api/admin/startups", headers=user),
                 lambda: client.post("/api/admin/startups", headers=user, json={"name": "x"}),
                 lambda: client.get("/api/admin/startups.csv", headers=user)):
        assert call().status_code == 403


def test_browse_and_filter(client, admin):
    h, _ = admin
    d = find(client, h)
    assert {r["name"] for r in d["rows"]} == {f"{PREFIX} Alpha", f"{PREFIX} Beta"}
    assert [r["name"] for r in find(client, h, focus="yes")["rows"]] == [f"{PREFIX} Alpha"]
    assert [r["name"] for r in find(client, h, stage="series a")["rows"]] == [f"{PREFIX} Beta"]
    assert [r["name"] for r in find(client, h, city="mumbai")["rows"]] == [f"{PREFIX} Beta"]
    alpha = find(client, h, focus="yes")["rows"][0]
    assert "no_founder_email" in alpha["quality"] and alpha["in_focus"]
    assert d["summary"]["issues"]["no_founder_email"] >= 2


def test_edit_survives_the_morning_refresh(client, admin):
    h, _ = admin
    sid = find(client, h, focus="yes")["rows"][0]["id"]
    r = client.patch(f"/api/admin/startups/{sid}", headers=h, json={"one_liner": "Credit for kirana stores",
                                                                     "founder_name": "Asha Rao", "founder_email": "asha@zst-alpha.in"})
    assert r.status_code == 200 and r.json()["founder_email"] == "asha@zst-alpha.in"
    assert set(r.json()["edited"]) >= {"one_liner", "founders"}
    with SessionLocal() as db:
        discovery.upsert_startups(db, [{"name": f"{PREFIX} Alpha", "domain": "zst-alpha.in", "one_liner": "Something else",
                                        "signal": "raised a seed round", "source": "news"}])
        s = db.get(Startup, sid)
        assert s.one_liner == "Credit for kirana stores" and s.signal == "raised a seed round"  # edit kept, the rest updates
    assert client.patch(f"/api/admin/startups/{sid}", headers=h, json={"founder_email": "not-an-email"}).status_code == 400
    assert client.patch(f"/api/admin/startups/{sid}", headers=h, json={"domain": "zst-beta.in"}).status_code == 400  # taken


def test_hidden_never_reaches_users(client, admin):
    h, _ = admin
    sid = find(client, h, focus="yes")["rows"][0]["id"]
    with SessionLocal() as db:
        assert f"{PREFIX} Alpha" in {c["name"] for c in matching.public_fresh(db, limit=1000)}
    r = client.patch(f"/api/admin/startups/{sid}", headers=h, json={"hidden": True, "hidden_reason": "shut down"})
    assert r.json()["hidden"] and r.json()["hidden_reason"] == "shut down"
    with SessionLocal() as db:
        assert f"{PREFIX} Alpha" not in {c["name"] for c in matching.public_fresh(db, limit=1000)}
    assert find(client, h)["total"] == 1 and find(client, h, hidden="only")["rows"][0]["id"] == sid
    client.patch(f"/api/admin/startups/{sid}", headers=h, json={"hidden": False})
    assert find(client, h)["total"] == 2


def test_add_by_hand(client, admin):
    h, _ = admin
    r = client.post("/api/admin/startups", headers=h, json={"name": f"{PREFIX} Gamma", "domain": "https://www.zst-gamma.ai/",
                                                             "stage": "Seed", "location": "Indiranagar, Bengaluru"})
    assert r.status_code == 200 and r.json()["domain"] == "zst-gamma.ai" and r.json()["stage"] == "seed" and r.json()["in_focus"]
    assert client.post("/api/admin/startups", headers=h, json={"name": f"{PREFIX} Dupe", "domain": "zst-gamma.ai"}).status_code == 400
    assert client.post("/api/admin/startups", headers=h, json={"name": "  "}).status_code == 400


def test_csv_round_trip(client, admin):
    h, _ = admin
    r = client.get("/api/admin/startups.csv", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert any(x["name"] == f"{PREFIX} Alpha" and x["domain"] == "zst-alpha.in" for x in rows)

    upload = ("name,domain,stage,location,founder_name,founder_email\n"
              f"{PREFIX} Delta,zst-delta.in,pre-seed,\"Koramangala, Bengaluru\",Ravi K,ravi@zst-delta.in\n"
              f"{PREFIX} Alpha,zst-alpha.in,,,Asha Rao,asha@zst-alpha.in\n"
              f"{PREFIX} Bad,zst-bad.in,seed,Bengaluru,X,not-an-email\n"
              ",nameless.in,seed,,,\n")
    res = client.post("/api/admin/startups/import", headers=h, files={"file": ("s.csv", upload.encode())}).json()
    assert res["added"] == 1 and res["updated"] == 1 and len(res["errors"]) == 1 and "Bad" in res["errors"][0]
    delta = find(client, h, q=f"{PREFIX} Delta")["rows"][0]
    assert delta["source"] == "import" and delta["founder_email"] == "ravi@zst-delta.in" and delta["in_focus"]
    bad = client.post("/api/admin/startups/import", headers=h, files={"file": ("x.csv", b"company,website\nA,a.in\n")})
    assert bad.status_code == 400 and "'name' column" in bad.json()["detail"]


def test_recheck_validates_the_gap(client, admin, monkeypatch):
    h, _ = admin
    monkeypatch.setattr(main, "_recheck", lambda ids: None)  # no network
    assert client.post("/api/admin/startups/recheck", headers=h, json={"issue": "everything"}).status_code == 422
    assert client.post("/api/admin/startups/recheck", headers=h, json={"issue": "no_founder_email"}).json()["ok"]
