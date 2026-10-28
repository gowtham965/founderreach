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

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import agent, pages, scheduler
from app.auth import create_token, current_user, decode_payload, decode_token, hash_password, is_admin, session_token, verify_password
from app.config import get_settings
from app.db import SessionLocal, get_db, init_db
from app.models import ChatMessage, Match, Payment, Startup, User
from app.services import billing, discovery, email_check, google_login, mailer, matching, outreach, profile as profile_svc
from app.services import wa_bot, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).resolve().parents[2] / "frontend"  # served at /static


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    if s.secret_key == "change-me-in-production" and s.llm_provider != "fake":
        raise RuntimeError("Set SECRET_KEY in .env (run.sh generates one) before running with real users")
    init_db()
    scheduler.start()
    if s.scheduler_enabled:
        _fill_empty_pool()
    yield
    scheduler.stop()


def _fill_empty_pool() -> None:
    """A brand-new server has no startups until the 7am job; fetch them now, in the background."""
    with SessionLocal() as db:
        if db.scalar(select(func.count(Startup.id))):
            return

    def work():
        with SessionLocal() as db:
            logging.getLogger(__name__).info("startup pool empty: running discovery now (%d added)",
                                             discovery.run_discovery(db))

    threading.Thread(target=work, daemon=True).start()


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)

# Content Security Policy: only our own scripts; styles and fonts from us and Google Fonts;
# never embeddable in another site's frame.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://static.cloudflareinsights.com",  # Cloudflare Web Analytics (no cookies)
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",  # inline style attributes are used
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self' https://cloudflareinsights.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if get_settings().base_url.startswith("https://"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.api_route("/", include_in_schema=False, methods=["GET", "HEAD"])
def index():
    v = pages.asset_v()  # versioned links: after a deploy, browsers load the new CSS and JS
    return HTMLResponse((STATIC / "index.html").read_text()
                        .replace('/static/styles.css"', f'/static/styles.css?v={v}"')
                        .replace('/static/app.js"', f'/static/app.js?v={v}"')
                        .replace("</body>", pages.analytics_tag() + "</body>"))


# ---------- auth ----------
class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str = Field("", max_length=100)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/auth/signup")
def signup(body: SignupIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    if not email_signup_open():
        raise HTTPException(503, "Email sign-up opens soon. For now, continue with Google.")
    if _too_many_signups(client_ip(request)):  # also stops using us to send confirmation emails to strangers
        raise HTTPException(429, "Too many new accounts from this network. Try again in an hour.")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with that email already exists")
    if problem := email_check.check(email):
        raise HTTPException(400, problem)
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    _count_signup(client_ip(request))
    _welcome(db, user)
    _send_confirmation(db, user, background)
    return {"token": session_token(user)}


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
    if not mailer.is_configured() and get_settings().llm_provider != "fake":
        raise HTTPException(503, "Confirmation emails aren't available yet. Sign in with Google using this email to unlock.")
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


def client_ip(request: Request) -> str:
    """The visitor's IP. Each proxy in front of us appends the address it saw to X-Forwarded-For;
    anything further left was written by the visitor and can be faked. TRUSTED_PROXY_HOPS = how many
    proxies append (Render: 2, Cloudflare then its load balancer). Check it with GET /api/public/ip."""
    hops = get_settings().trusted_proxy_hops
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if hops and len(forwarded) >= hops:
        return forwarded[-hops]
    return request.client.host if request.client else "?"


_SIGNUPS: dict[str, list[float]] = {}


def _too_many_signups(ip: str, per_hour: int = 5) -> bool:
    """Accounts created from this IP in the last hour (failed attempts, like a typo, don't count)."""
    import time
    now_ts = time.monotonic()
    recent = [t for t in _SIGNUPS.get(ip, []) if now_ts - t < 3600]
    _SIGNUPS[ip] = recent
    return len(recent) >= per_hour


def _count_signup(ip: str) -> None:
    import time
    if len(_SIGNUPS) > 20000:  # keep memory bounded
        _SIGNUPS.clear()
    _SIGNUPS.setdefault(ip, []).append(time.monotonic())


# Failed sign-ins in 15 minutes: 5 per email from one IP (so a stranger can't lock you out
# from your own network), 50 per email overall (guessing spread across many IPs), 20 per IP.
_FAILS: dict[str, list[float]] = {}
_FAIL_WINDOW, _MAX_PER_EMAIL_IP, _MAX_PER_EMAIL, _MAX_PER_IP = 15 * 60, 5, 50, 20


def _recent_fails(key: str) -> list[float]:
    import time
    cutoff = time.monotonic() - _FAIL_WINDOW
    fails = [t for t in _FAILS.get(key, []) if t > cutoff]
    if fails:
        _FAILS[key] = fails
    else:
        _FAILS.pop(key, None)
    return fails


def _login_blocked(email: str, ip: str) -> int:
    """Seconds to wait, or 0 if this sign-in may be attempted."""
    import time
    for key, limit in ((f"ei:{email}|{ip}", _MAX_PER_EMAIL_IP), (f"e:{email}", _MAX_PER_EMAIL), (f"i:{ip}", _MAX_PER_IP)):
        fails = _recent_fails(key)
        if len(fails) >= limit:
            return int(fails[0] + _FAIL_WINDOW - time.monotonic()) + 1
    return 0


def _record_fail(email: str, ip: str) -> None:
    import time
    if len(_FAILS) > 20000:  # keep memory bounded
        _FAILS.clear()
    for key in (f"ei:{email}|{ip}", f"e:{email}", f"i:{ip}"):
        _FAILS.setdefault(key, []).append(time.monotonic())


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email, ip = body.email.lower(), client_ip(request)
    if wait := _login_blocked(email, ip):
        raise HTTPException(429, f"Too many sign-in attempts. Try again in {max(1, round(wait / 60))} minute(s).")
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(body.password, user.password_hash):
        _record_fail(email, ip)
        raise HTTPException(401, "Wrong email or password")
    _FAILS.pop(f"ei:{email}|{ip}", None)  # a correct password clears this IP's count for the account
    return {"token": session_token(user)}


# ---------- Sign in with Google ----------
_GOOGLE_STATE_COOKIE = "fr_google_state"


def email_signup_open() -> bool:
    """Email sign-up needs confirmation emails; without SMTP nobody could ever confirm.
    The offline demo (fake AI) keeps it open for local testing."""
    return mailer.is_configured() or get_settings().llm_provider == "fake"


@app.get("/api/auth/providers")
def auth_providers():
    return {"google": google_login.is_configured(), "whatsapp": whatsapp.chat_link(), "email_signup": email_signup_open()}


@app.get("/api/auth/magic", include_in_schema=False)
def magic_login(token: str = "", db: Session = Depends(get_db)):
    """Sign-in link sent over WhatsApp (15 minutes); lands in the web app signed in."""
    try:
        payload = decode_payload(token, purpose="magic")
    except HTTPException:
        return RedirectResponse("/?magic=expired")
    user = db.get(User, int(payload["sub"]))
    if user is None or payload.get("ep", 0) != (user.session_epoch or 0):
        return RedirectResponse("/?magic=expired")
    return RedirectResponse(f"/#token={session_token(user)}")


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
            resp = RedirectResponse(f"/#token={session_token(user)}")
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
            "is_admin": is_admin(user), "can_confirm": email_signup_open(), "billing": billing.summary(user),
            "profile": {"summary": p.summary, "skills": p.skills, "highlights": p.highlights,
                        "target_roles": p.target_roles, "preferences": p.preferences, "ready": p.is_ready()}}


@app.delete("/api/me")
def delete_account(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """'Delete everything in one tap.'"""
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete()
    # payment records stay (refunds, accounts) but no longer point at the account
    db.query(Payment).filter(Payment.user_id == user.id).update({Payment.user_id: None})
    for m in db.scalars(select(Match).where(Match.user_id == user.id)).all():
        db.delete(m)
    db.delete(user)
    db.commit()
    return {"ok": True}


# ---------- chat ----------
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@app.get("/api/chat")
def chat_history(after: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The last 100 messages, or with ?after=<id> only newer ones (what the 30-second poll asks for)."""
    q = select(ChatMessage).where(ChatMessage.user_id == user.id)
    if after:
        rows = db.scalars(q.where(ChatMessage.id > after).order_by(ChatMessage.id).limit(100)).all()
    else:
        rows = list(reversed(db.scalars(q.order_by(ChatMessage.id.desc()).limit(100)).all()))
    return [{"id": r.id, "role": r.role, "content": r.content, "unread": r.unread, "at": r.created_at.isoformat(),
             "cards": agent.rebuild_cards(db, user, r.cards)} for r in rows]


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
    try:
        p, kind = profile_svc.ingest_document(db, user, text)
        msg = profile_svc.upload_reply(p, kind)
    except Exception:  # AI unavailable: keep the conversation whole and say so
        logging.getLogger(__name__).exception("resume reading failed for user %s", user.id)
        db.rollback()
        msg = "I couldn't read it right now because my AI side is unavailable. Please attach it again in a few minutes."
    db.add(ChatMessage(user_id=user.id, role="assistant", content=msg))
    db.commit()
    return {"reply": msg, "profile": profile_svc.profile_brief(user.profile)}


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
    except billing.PaywallError as exc:
        raise HTTPException(402, str(exc)) from exc
    except outreach.OutreachError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # e.g. the AI is unavailable while drafting
        logging.getLogger(__name__).exception("action failed")
        raise HTTPException(503, "That didn't work right now. Please try again in a few minutes.") from exc


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


# ---------- passes & payments (Razorpay or PhonePe) ----------
class PayIn(BaseModel):
    plan: str


@app.post("/api/pay")
def api_pay(body: PayIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Start a payment; the browser goes to the gateway's payment page."""
    try:
        return {"redirect_url": billing.create_order(db, user, body.plan)}
    except billing.PaymentError as exc:
        raise HTTPException(400 if not billing.payments_on() or body.plan not in billing.PLANS else 503, str(exc)) from exc
    except httpx.HTTPError as exc:
        logging.getLogger(__name__).exception("payment gateway unreachable")
        raise HTTPException(503, "Couldn't reach the payment gateway. Please try again in a few minutes.") from exc


@app.get("/pay/return", include_in_schema=False)
def pay_return(o: str = ""):
    """The gateway sends the browser back here (Razorpay adds its own query fields, which we don't need: the
    status is fetched from the gateway). The app then checks the order (signed in) and shows the result."""
    return RedirectResponse(f"/?paid={o}" if billing.ORDER_ID_RE.match(o) else "/", status_code=303)


@app.post("/api/pay/{order_id}/check")
def api_pay_check(order_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pay = db.scalar(select(Payment).where(Payment.merchant_order_id == order_id, Payment.user_id == user.id))
    if pay is None:
        raise HTTPException(404, "No such payment")
    try:
        pay = billing.refresh(db, pay)
    except (billing.PaymentError, httpx.HTTPError):
        logging.getLogger(__name__).exception("payment status check failed")
    db.refresh(user)
    return {"state": pay.state, "plan": pay.plan, "billing": billing.summary(user)}


@app.post("/api/pay/webhook", include_in_schema=False)
async def pay_webhook(request: Request, db: Session = Depends(get_db)):
    """PhonePe's server-to-server notice. The body is only a hint: the status comes from PhonePe's API."""
    if not billing.phonepe_webhook_ok(request.headers.get("authorization")):
        raise HTTPException(401, "bad signature")
    try:
        data = json.loads(await request.body())
    except ValueError:
        raise HTTPException(400, "bad json")
    order_id = str(((data.get("payload") or {}) if isinstance(data, dict) else {}).get("merchantOrderId", ""))
    pay = db.scalar(select(Payment).where(Payment.merchant_order_id == order_id)) if billing.ORDER_ID_RE.match(order_id) else None
    if pay is not None:
        try:
            billing.refresh(db, pay)
        except (billing.PaymentError, httpx.HTTPError):
            logging.getLogger(__name__).exception("PhonePe status check failed (webhook)")
            raise HTTPException(503, "try again")  # PhonePe retries
    return {"ok": True}


@app.post("/api/pay/razorpay-webhook", include_in_schema=False)
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Razorpay's server-to-server notice (payment_link.paid and friends). Signed over the raw body; the
    status itself is then fetched from Razorpay's API."""
    body = await request.body()
    if not billing.razorpay_webhook_ok(body, request.headers.get("x-razorpay-signature")):
        raise HTTPException(401, "bad signature")
    try:
        data = json.loads(body)
        link = data["payload"]["payment_link"]["entity"]
        order_id = str(link.get("reference_id") or "")
    except (ValueError, KeyError, TypeError):
        return {"ok": True}  # other events (payments, refunds) carry no link: nothing to do
    pay = db.scalar(select(Payment).where(Payment.merchant_order_id == order_id)) if billing.ORDER_ID_RE.match(order_id) else None
    if pay is not None:
        try:
            billing.refresh(db, pay)
        except (billing.PaymentError, httpx.HTTPError):
            logging.getLogger(__name__).exception("Razorpay status check failed (webhook)")
            raise HTTPException(503, "try again")  # Razorpay retries
    return {"ok": True}


# ---------- admin ----------
@app.post("/api/admin/discover")
def run_discovery_now(user: User = Depends(current_user)):
    """Kick off a discovery run in the background. Admins only: each run can spend AI credits."""
    if not is_admin(user):
        raise HTTPException(403, "Only the FounderReach admin can refresh the startup pool")
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


@app.get("/api/public/ip")
def public_ip(request: Request):
    """The address rate limits see for you (checks the proxy setup; it's only your own IP)."""
    return {"ip": client_ip(request)}


@app.get("/api/public/stats")
def public_stats(db: Session = Depends(get_db)):
    return matching.public_stats(db)


@app.get("/api/public/fresh")
def public_fresh(db: Session = Depends(get_db)):
    return matching.public_fresh(db)


@app.get("/api/public/preview")
def public_preview(request: Request, q: str = "", db: Session = Depends(get_db)):
    if _rate_limited(client_ip(request)):
        raise HTTPException(429, "Slow down a little and try again in a minute.")
    return matching.public_preview(db, q[:200])


# ---------- policy pages ----------
@app.api_route("/terms", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def terms_page():
    return pages.terms()


@app.api_route("/privacy", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def privacy_page():
    return pages.privacy()


@app.api_route("/refunds", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def refunds_page():
    return pages.refunds()


@app.api_route("/contact", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def contact_page():
    return pages.contact()


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "brand" / "favicon.ico", media_type="image/x-icon")


@app.api_route("/healthz", include_in_schema=False, methods=["GET", "HEAD"])
def health():
    return HTMLResponse("ok")
