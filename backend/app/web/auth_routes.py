"""Accounts: email sign-up and sign-in, email confirmation, Sign in with Google, WhatsApp magic links."""
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import create_token, current_user, decode_payload, decode_token, hash_password, session_token, verify_password
from app.config import get_settings
from app.db import get_db
from app.models import ChatMessage, User
from app.services import alerts, discovery, email_check, google_login, mailer, whatsapp
from app.services.billing import ledger
from app.web.security import clear_fails, client_ip, count_signup, login_blocked, record_fail, too_many_signups

router = APIRouter()
log = logging.getLogger(__name__)

_CONFIRM_MINUTES = 60 * 24
_RESEND_AFTER = timedelta(seconds=60)
_GOOGLE_STATE_COOKIE = "fr_google_state"


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str = Field("", max_length=100)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


def email_signup_open() -> bool:
    """Email sign-up needs confirmation emails; without SMTP nobody could ever confirm.
    The offline demo (fake AI) keeps it open for local testing."""
    return mailer.is_configured() or get_settings().llm_provider == "fake"


def welcome(db: Session, user: User) -> None:
    first = (user.name or "there").split()[0]
    db.add(ChatMessage(user_id=user.id, role="assistant", content=(
        f"hey {first} 👋 I find {discovery.focus_label()} that just raised and fit you, "
        "and help you email their founders directly.\n\n"
        "Attach your resume or LinkedIn PDF (paperclip below), paste your LinkedIn profile link, and tell me what you want next: "
        "role, sectors, remote or in-office.")))
    db.commit()


def _send_confirmation(db: Session, user: User, background: BackgroundTasks) -> None:
    token = create_token(user.id, purpose="verify_email", minutes=_CONFIRM_MINUTES)
    link = f"{get_settings().base_url.rstrip('/')}/api/auth/verify?token={token}"
    user.verification_sent_at = datetime.now(timezone.utc)
    db.commit()
    background.add_task(mailer.send_confirmation, user.email, user.name, link)


@router.post("/api/auth/signup")
def signup(body: SignupIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    if not email_signup_open():
        raise HTTPException(503, "Email sign-up opens soon. For now, continue with Google.")
    if too_many_signups(client_ip(request)):  # also stops using us to send confirmation emails to strangers
        raise HTTPException(429, "Too many new accounts from this network. Try again in an hour.")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with that email already exists")
    if problem := email_check.check(email):
        raise HTTPException(400, problem)
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    ledger.restore(db, user)  # same person as an earlier (deleted) account: the trial continues, it doesn't restart
    count_signup(client_ip(request))
    alerts.signup(user.email, "email")
    welcome(db, user)
    _send_confirmation(db, user, background)
    return {"token": session_token(user)}


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email, ip = body.email.lower(), client_ip(request)
    if wait := login_blocked(email, ip):
        raise HTTPException(429, f"Too many sign-in attempts. Try again in {max(1, round(wait / 60))} minute(s).")
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(body.password, user.password_hash):
        record_fail(email, ip)
        raise HTTPException(401, "Wrong email or password")
    clear_fails(email, ip)
    return {"token": session_token(user)}


@router.get("/api/auth/verify", include_in_schema=False)
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


@router.post("/api/auth/resend-verification")
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


@router.get("/api/auth/providers")
def auth_providers():
    return {"google": google_login.is_configured(), "whatsapp": whatsapp.chat_link(), "email_signup": email_signup_open()}


@router.get("/api/auth/magic", include_in_schema=False)
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


@router.get("/api/auth/google", include_in_schema=False)
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


@router.get("/api/auth/google/callback", include_in_schema=False)
def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    db: Session = Depends(get_db)):
    if error:
        resp = RedirectResponse("/?google=denied")
    else:
        expected = request.cookies.get(_GOOGLE_STATE_COOKIE, "")
        try:
            if not (code and expected and hmac.compare_digest(state, expected)):
                raise google_login.GoogleLoginError("state mismatch")
            identity = google_login.fetch_identity(code)
            if not google_login.existing_user(db, identity) and too_many_signups(client_ip(request)):
                resp = RedirectResponse("/?google=limit")  # new accounts only; existing ones always sign in
                resp.delete_cookie(_GOOGLE_STATE_COOKIE, path="/api/auth/google")
                return resp
            user, created = google_login.sign_in(db, identity)
            if created:
                ledger.restore(db, user)
                count_signup(client_ip(request))
                welcome(db, user)
            # token goes in the #fragment so it never reaches server logs; app.js picks it up
            resp = RedirectResponse(f"/#token={session_token(user)}")
        except google_login.GoogleLoginError as exc:
            log.warning("Google sign-in failed: %s", exc)
            resp = RedirectResponse("/?google=error")
    resp.delete_cookie(_GOOGLE_STATE_COOKIE, path="/api/auth/google")
    return resp
