"""WhatsApp-first: number = account, replies only (never first), retries ignored, dashboard link, drafts to send yourself."""
import hashlib
import hmac
import json
import os
import tempfile
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("NEWS_FEEDS", "")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.auth import decode_token  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ChatMessage, User, now  # noqa: E402
from app.services import outreach, whatsapp  # noqa: E402

SECRET = "test-app-secret"
LINKEDIN = ("Contact\nwww.linkedin.com/in/meera-blr (LinkedIn)\nTop Skills\nPython\nMeera Nair\nBackend Engineer\n"
            "Built payment systems at Acme that cut failures 30%.\nPage 1 of 2\n") * 3


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def outbox(monkeypatch):
    """Meta's API stand-in: records every message we'd send."""
    s = get_settings()
    for k, v in {"whatsapp_token": "tok", "whatsapp_phone_number_id": "PNID", "whatsapp_app_secret": SECRET,
                 "whatsapp_verify_token": "verify-me", "whatsapp_number": "+1 555 000 1111"}.items():
        monkeypatch.setattr(s, k, v)
    sent = []

    class Resp:
        status_code, text = 200, "{}"

    def post(url, headers=None, json=None, timeout=None):
        assert url.endswith("/PNID/messages") and headers["Authorization"] == "Bearer tok"
        sent.append({"to": json["to"], "text": json["text"]["body"]})
        return Resp()

    monkeypatch.setattr(whatsapp.httpx, "post", post)
    return sent


_n = 0


def webhook(client, phone, text=None, msg_id=None, document=None, name="Meera Nair", secret=SECRET):
    global _n
    _n += 1
    msg = {"from": phone, "id": msg_id or f"wamid.{_n}", "timestamp": "1", "type": "document" if document else "text"}
    if document:
        msg["document"] = document
    else:
        msg["text"] = {"body": text}
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "contacts": [{"wa_id": phone, "profile": {"name": name}}], "messages": [msg]}}]}]}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/api/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def user_by_phone(phone):
    with SessionLocal() as db:
        return db.query(User).filter_by(phone=phone).one()


def test_webhook_verification(client, outbox):
    ok = client.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_bad_signature_rejected(client, outbox):
    r = webhook(client, "919800000001", "hi", secret="wrong")
    assert r.status_code == 401 and outbox == []


def test_first_message_creates_account_and_welcomes(client, outbox):
    assert webhook(client, "919800000002", "Hi FounderReach").status_code == 200
    u = user_by_phone("919800000002")
    assert u.name == "Meera Nair" and u.email.endswith("@whatsapp.invalid") and u.email_verified_at and u.password_hash == ""
    assert len(outbox) == 1 and outbox[0]["to"] == "919800000002"
    assert "resume" in outbox[0]["text"] and "LinkedIn PDF" in outbox[0]["text"]


def test_retried_webhook_is_answered_once(client, outbox):
    webhook(client, "919800000003", "hello", msg_id="wamid.same")
    webhook(client, "919800000003", "hello", msg_id="wamid.same")
    assert len(outbox) == 1


def test_text_goes_to_the_agent(client, outbox):
    webhook(client, "919800000004", "hi")
    webhook(client, "919800000004", "I'm a backend engineer looking for fintech roles")
    assert len(outbox) == 2 and outbox[1]["text"]
    with SessionLocal() as db:
        u = db.query(User).filter_by(phone="919800000004").one()
        roles = [m.role for m in db.query(ChatMessage).filter_by(user_id=u.id)]
    assert "user" in roles  # same chat history as the website


def test_linkedin_pdf_document(client, outbox, monkeypatch):
    monkeypatch.setattr(whatsapp, "download_media", lambda media_id: (LINKEDIN.encode(), "text/plain"))
    webhook(client, "919800000005", "hi")
    webhook(client, "919800000005", document={"id": "MEDIA1", "filename": "Profile.txt", "mime_type": "text/plain"})
    assert outbox[-1]["text"].startswith("read your LinkedIn profile ✅")
    with SessionLocal() as db:
        u = db.query(User).filter_by(phone="919800000005").one()
        assert u.profile.links["linkedin"] == "https://www.linkedin.com/in/meera-blr"


def test_unsupported_file_type(client, outbox):
    webhook(client, "919800000006", "hi")
    webhook(client, "919800000006", document={"id": "M2", "filename": "photo.jpg", "mime_type": "image/jpeg"})
    assert "PDF or text" in outbox[-1]["text"]


def test_never_messages_first(client, outbox):
    """Things that happen between messages wait; they arrive with the next message."""
    webhook(client, "919800000007", "hi")
    u = user_by_phone("919800000007")
    with SessionLocal() as db:
        outreach.notify(db, u.id, "🎉 Priya from Kirana AI replied")
    assert len(outbox) == 1  # nothing sent on its own
    webhook(client, "919800000007", "gm")
    assert "While you were away" in outbox[-1]["text"] and "Priya from Kirana AI replied" in outbox[-1]["text"]
    webhook(client, "919800000007", "anything new?")
    assert "While you were away" not in outbox[-1]["text"]  # delivered once


def test_send_refused_outside_24h_window(outbox):
    with SessionLocal() as db:
        u = User(email="wa-x@whatsapp.invalid", password_hash="", phone="919800000008",
                 whatsapp_last_inbound_at=now() - timedelta(hours=25))
        db.add(u)
        db.commit()
        with pytest.raises(whatsapp.WhatsAppError, match="never messages first"):
            whatsapp.send_text(u, "hello?")
    assert outbox == []


def test_dashboard_link_signs_in(client, outbox):
    webhook(client, "919800000009", "hi")
    webhook(client, "919800000009", "dashboard")
    link = next(w for w in outbox[-1]["text"].split() if "/api/auth/magic?token=" in w)
    r = client.get(urlparse(link).path + "?" + urlparse(link).query, follow_redirects=False)
    session = r.headers["location"].split("#token=")[1]
    assert decode_token(session) == user_by_phone("919800000009").id
    assert client.get("/api/auth/magic?token=garbage", follow_redirects=False).headers["location"] == "/?magic=expired"


def test_gmail_command_is_gone(client, outbox):
    """FounderReach never connects to Gmail: "gmail" is just a chat message now."""
    webhook(client, "919800000010", "hi")
    webhook(client, "919800000010", "gmail")
    assert "accounts.google.com" not in outbox[-1]["text"]


def test_render_cards():
    match = {"match_id": 7, "name": "Kirana AI", "location": "HSR Layout, Bengaluru", "stage": "seed",
             "one_liner": "AI for kirana stores", "score": 88, "reason": "You built payments", "outreach": None}
    text = whatsapp.render_cards([{"type": "matches", "items": [match]}])
    assert "*#7 Kirana AI* (88 fit)" in text and "HSR Layout · seed" in text and "draft #12" in text
    links = outreach.compose_links("priya@kirana.ai", "Hi", "Hello Priya")
    draft = dict(match, outreach={"status": "draft", "to_email": "priya@kirana.ai", "to_name": "Priya", "to_confidence": "verified",
                                  "subject": "Hi", "body": "Hello Priya", "compose": links})
    text = whatsapp.render_cards([{"type": "draft", "item": draft}])
    assert "To: Priya <priya@kirana.ai> · verified ✓" in text and links["gmail"] in text and "reply *sent*" in text
    sent = dict(match, outreach={"status": "sent", "to_email": "priya@kirana.ai", "to_name": "Priya"})
    assert whatsapp.render_cards([{"type": "draft", "item": sent}]).startswith("✅ Marked as sent to Priya")
    fu_links = outreach.compose_links("priya@kirana.ai", "Re: Hi", "Bumping this")
    due = dict(match, outreach={"status": "sent", "to_email": "priya@kirana.ai", "followup_draft": "Bumping this",
                                "followup_compose": fu_links})
    text = whatsapp.render_cards([{"type": "draft", "item": due}])
    assert "Follow-up for Kirana AI" in text and fu_links["gmail"] in text and "*follow-up sent*" in text


def test_split_long_messages():
    parts = whatsapp.split_text(("para " * 300 + "\n\n") * 5)
    assert len(parts) > 1 and all(len(p) <= whatsapp.MAX_TEXT for p in parts)


def test_chat_link_and_providers(client, outbox):
    assert whatsapp.chat_link() == "https://wa.me/15550001111?text=Hi%20FounderReach"
    assert client.get("/api/auth/providers").json()["whatsapp"].startswith("https://wa.me/15550001111")


def test_startup_map_matches_are_credited_with_a_link():
    match = {"match_id": 3, "name": "MapCo", "location": "HSR Layout, Bengaluru", "stage": "seed", "one_liner": "x",
             "score": 80, "reason": "fits", "source": "startup_map",
             "source_url": "https://www.bangalorestartupmap.com/company/mapco"}
    text = whatsapp.render_cards([{"type": "matches", "items": [match]}])
    assert "via Bangalore Startup Map: https://www.bangalorestartupmap.com/company/mapco" in text
    other = dict(match, source="news", source_url="https://inc42.com/x")
    assert "Bangalore Startup Map" not in whatsapp.render_cards([{"type": "matches", "items": [other]}])
