"""HTTP API + static web app.

Run:  uvicorn app.main:app --reload
"""
import hmac
import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agent, scheduler
from app.auth import create_token, current_user, decode_token, hash_password, verify_password
from app.config import get_settings
from app.db import SessionLocal, get_db, init_db
from app.models import ChatMessage, Match, User
from app.services import discovery, email_check, google_login, mailer, matching, outreach, profile as profile_svc
from app.services import wa_bot, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    if s.secret_key == "change-me-in-production" and s.llm_provider != "fake":
        raise RuntimeError("Set SECRET_KEY in .env (run.sh generates one) before running with real users")
    init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# ---------- auth ----------
class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str = ""


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/auth/signup")
def signup(body: SignupIn, background: BackgroundTasks, db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with that email already exists")
    if problem := email_check.check(email):
        raise HTTPException(400, problem)
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    _welcome(db, user)
    _send_confirmation(db, user, background)
    return {"token": create_token(user.id)}


# ---------- email confirmation ----------
_CONFIRM_MINUTES = 60 * 24
_RESEND_AFTER = timedelta(seconds=60)


def _send_confirmation(db: Session, user: User, background: BackgroundTasks) -> None:
    token = create_token(user.id, purpose="verify_email", minutes=_CONFIRM_MINUTES)
    link = f"{get_settings().base_url.rstrip('/')}/api/auth/verify?token={token}"
    user.verification_sent_at = datetime.now(timezone.utc)
    db.commit()
    background.add_task(mailer.send_confirmation, user.email, user.name, link)


@app.get("/api/auth/verify", include_in_schema=False)
def verify_email(token: str = "", db: Session = Depends(get_db)):
    """The link in the confirmation email. Opened in a browser, so answer with a redirect, not JSON."""
    try:
        user = db.get(User, decode_token(token, purpose="verify_email"))
    except HTTPException:
        return RedirectResponse("/?verified=expired")
    if user is None:
        return RedirectResponse("/?verified=expired")
    if not user.email_verified_at:
        user.email_verified_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/?verified=1")


@app.post("/api/auth/resend-verification")
def resend_verification(background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.email_verified_at:
        return {"ok": True, "already": True}
    last = user.verification_sent_at
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last and datetime.now(timezone.utc) - last < _RESEND_AFTER:
        raise HTTPException(429, "We just sent one. Give it a minute, and check your spam folder.")
    _send_confirmation(db, user, background)
    return {"ok": True}


def _welcome(db: Session, user: User) -> None:
    first = (user.name or "there").split()[0]
    db.add(ChatMessage(user_id=user.id, role="assistant", content=(
        f"hey {first} 👋 I find {discovery.focus_label()} that just raised and fit you, "
        "and help you email their founders directly.\n\n"
        "Attach your resume or LinkedIn PDF (paperclip below), paste your LinkedIn profile link, and tell me what you want next: "
        "role, sectors, remote or in-office.")))
    db.commit()


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong email or password")
    return {"token": create_token(user.id)}


# ---------- Sign in with Google ----------
_GOOGLE_STATE_COOKIE = "fr_google_state"


@app.get("/api/auth/providers")
def auth_providers():
    return {"google": google_login.is_configured(), "whatsapp": whatsapp.chat_link()}


@app.get("/api/auth/magic", include_in_schema=False)
def magic_login(token: str = ""):
    """One-time-ish sign-in link sent over WhatsApp (15 minutes); lands in the web app signed in."""
    try:
        user_id = decode_token(token, purpose="magic")
    except HTTPException:
        return RedirectResponse("/?magic=expired")
    return RedirectResponse(f"/#token={create_token(user_id)}")


@app.get("/api/auth/google", include_in_schema=False)
def google_start():
    state = secrets.token_urlsafe(32)
    try:
        resp = RedirectResponse(google_login.auth_url(state))
    except google_login.GoogleLoginError as exc:
        raise HTTPException(503, str(exc)) from exc
    # Google echoes the state back; it must match this cookie, so nobody can log you into their account
    resp.set_cookie(_GOOGLE_STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=get_settings().base_url.startswith("https"), path="/api/auth/google")
    return resp


@app.get("/api/auth/google/callback", include_in_schema=False)
def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    db: Session = Depends(get_db)):
    if error:
        resp = RedirectResponse("/?google=denied")
    else:
        expected = request.cookies.get(_GOOGLE_STATE_COOKIE, "")
        try:
            if not (code and expected and hmac.compare_digest(state, expected)):
                raise google_login.GoogleLoginError("state mismatch")
            user, created = google_login.sign_in(db, google_login.fetch_identity(code))
            if created:
                _welcome(db, user)
            # token goes in the #fragment so it never reaches server logs; app.js picks it up
            resp = RedirectResponse(f"/#token={create_token(user.id)}")
        except google_login.GoogleLoginError as exc:
            logging.getLogger(__name__).warning("Google sign-in failed: %s", exc)
            resp = RedirectResponse("/?google=error")
    resp.delete_cookie(_GOOGLE_STATE_COOKIE, path="/api/auth/google")
    return resp


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = profile_svc.get_or_create_profile(db, user)
    return {"email": user.email, "name": user.name,
            "email_verified": bool(user.email_verified_at), "linkedin": (p.links or {}).get("linkedin"),
            "profile": {"summary": p.summary, "skills": p.skills, "highlights": p.highlights,
                        "target_roles": p.target_roles, "preferences": p.preferences, "ready": p.is_ready()}}


@app.delete("/api/me")
def delete_account(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """'Delete everything in one tap.'"""
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete()
    for m in db.scalars(select(Match).where(Match.user_id == user.id)).all():
        db.delete(m)
    db.delete(user)
    db.commit()
    return {"ok": True}


# ---------- chat ----------
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@app.get("/api/chat")
def chat_history(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id).order_by(ChatMessage.id)).all()
    return [{"id": r.id, "role": r.role, "content": r.content, "unread": r.unread, "at": r.created_at.isoformat()} for r in rows[-100:]]


@app.post("/api/chat")
def chat(body: ChatIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return agent.respond(db, user, body.message.strip())


@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > 5_000_000:
        raise HTTPException(413, "Resume must be under 5 MB")
    text = profile_svc.document_text(data, file.filename or "")
    if len(text) < 200:
        raise HTTPException(422, "Couldn't read much text from that file. Is it a scanned image? Try a text PDF.")
    db.add(ChatMessage(user_id=user.id, role="user", content=f"[attached: {file.filename}]"))
    db.commit()
    p, kind = profile_svc.ingest_document(db, user, text)
    msg = profile_svc.upload_reply(p, kind)
    db.add(ChatMessage(user_id=user.id, role="assistant", content=msg))
    db.commit()
    return {"reply": msg, "profile": profile_svc.profile_brief(p)}


# ---------- dashboard & direct actions (UI buttons; these skip the LLM) ----------
@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms = db.scalars(select(Match).where(Match.user_id == user.id).order_by(Match.created_at.desc())).all()
    return {"stats": outreach.stats(db, user), "matches": [matching.match_card(m) for m in ms]}


class DraftEditIn(BaseModel):
    subject: str | None = None
    body: str | None = None
    to_email: EmailStr | None = None
    to_name: str | None = None


def _act(fn):
    try:
        return fn()
    except outreach.OutreachError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/matches/{match_id}/draft")
def api_draft(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _act(lambda: outreach.draft(db, user, match_id))
    return matching.match_card(o.match)


@app.patch("/api/matches/{match_id}/draft")
def api_edit_draft(match_id: int, body: DraftEditIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.to_email:
        _act(lambda: outreach.set_recipient(db, user, match_id, body.to_email, body.to_name or ""))
    o = _act(lambda: outreach.edit_draft(db, user, match_id, body.subject, body.body))
    return matching.match_card(o.match)


@app.post("/api/matches/{match_id}/sent")
def api_mark_sent(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The user sent the draft from their own email."""
    o = _act(lambda: outreach.mark_sent(db, user, match_id))
    return matching.match_card(o.match)


@app.post("/api/matches/{match_id}/replied")
def api_mark_replied(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _act(lambda: outreach.mark_replied(db, user, match_id))
    return matching.match_card(o.match)


@app.post("/api/matches/{match_id}/followup-sent")
def api_followup_sent(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _act(lambda: outreach.mark_followup_sent(db, user, match_id))
    return matching.match_card(o.match)


@app.post("/api/matches/{match_id}/dm")
def api_dm(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _act(lambda: outreach.make_dm(db, user, match_id))
    return matching.match_card(o.match)


@app.post("/api/matches/{match_id}/skip")
def api_skip(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _act(lambda: outreach.skip(db, user, match_id))
    return matching.match_card(m)


@app.post("/api/matches/{match_id}/stop-followups")
def api_stop(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _act(lambda: outreach.cancel_followups(db, user, match_id))
    return matching.match_card(o.match)


# ---------- admin ----------
@app.post("/api/admin/discover")
def run_discovery_now(user: User = Depends(current_user)):
    """Kick off a discovery run in the background (handy right after first setup)."""
    def work():
        with SessionLocal() as db:
            discovery.run_discovery(db)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "message": "Discovery started; new startups appear in a minute or two."}


# ---------- WhatsApp (Meta Cloud API webhook) ----------
@app.get("/api/whatsapp/webhook", include_in_schema=False)
def whatsapp_verify(request: Request):
    """Meta's one-time check when you save the webhook URL."""
    q = request.query_params
    token = get_settings().whatsapp_verify_token
    if q.get("hub.mode") == "subscribe" and token and hmac.compare_digest(q.get("hub.verify_token", ""), token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


def _handle_whatsapp(msg: dict, name: str) -> None:
    with SessionLocal() as db:
        wa_bot.handle(db, msg, name)


@app.post("/api/whatsapp/webhook", include_in_schema=False)
async def whatsapp_incoming(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not whatsapp.signature_ok(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Bad signature")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "Not JSON")
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:  # delivery "statuses" are ignored
                # answer after acknowledging: Meta retries webhooks that take too long
                background.add_task(_handle_whatsapp, msg, names.get(msg.get("from"), ""))
    return {"ok": True}


# ---------- public landing page data (no sign-in) ----------
_preview_hits: dict[str, list[float]] = {}


def _rate_limited(ip: str, per_minute: int = 30) -> bool:
    import time
    now_ts = time.monotonic()
    hits = [t for t in _preview_hits.get(ip, []) if now_ts - t < 60]
    hits.append(now_ts)
    _preview_hits[ip] = hits
    if len(_preview_hits) > 5000:  # keep memory bounded
        _preview_hits.clear()
    return len(hits) > per_minute


@app.get("/api/public/stats")
def public_stats(db: Session = Depends(get_db)):
    return matching.public_stats(db)


@app.get("/api/public/fresh")
def public_fresh(db: Session = Depends(get_db)):
    return matching.public_fresh(db)


@app.get("/api/public/preview")
def public_preview(request: Request, q: str = "", db: Session = Depends(get_db)):
    if _rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "Slow down a little and try again in a minute.")
    return matching.public_preview(db, q[:200])


@app.get("/healthz", include_in_schema=False)
def health():
    return HTMLResponse("ok")
