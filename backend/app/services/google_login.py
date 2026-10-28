"""Sign in with Google (OpenID Connect). Asks only for name + email.

FounderReach never asks for Gmail access (users send drafts themselves), so the
consent screen asks only for name and email and needs no Google security review.
"""
from urllib.parse import urlencode

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, now

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "openid email profile"


class GoogleLoginError(Exception):
    pass


def is_configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret)


def redirect_uri() -> str:
    return f"{get_settings().base_url}/api/auth/google/callback"


def auth_url(state: str) -> str:
    if not is_configured():
        raise GoogleLoginError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not configured")
    params = {"client_id": get_settings().google_client_id, "redirect_uri": redirect_uri(),
              "response_type": "code", "scope": SCOPE, "state": state, "prompt": "select_account"}
    return f"{AUTH_URI}?{urlencode(params)}"


def fetch_identity(code: str) -> dict:
    """Swap the one-time code for Google's signed ID token and return its verified claims."""
    s = get_settings()
    r = httpx.post(TOKEN_URI, timeout=15, data={
        "code": code, "client_id": s.google_client_id, "client_secret": s.google_client_secret,
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    if r.status_code != 200 or "id_token" not in r.json():
        raise GoogleLoginError(f"Google token exchange failed ({r.status_code})")
    try:
        # checks signature, expiry, issuer and that the token was issued for our client id
        return id_token.verify_oauth2_token(r.json()["id_token"], Request(), s.google_client_id)
    except ValueError as exc:
        raise GoogleLoginError(f"Invalid Google ID token: {exc}") from exc


def sign_in(db: Session, identity: dict) -> tuple[User, bool]:
    """Find or create the user for a verified Google identity. Returns (user, created)."""
    sub, email = identity.get("sub"), (identity.get("email") or "").lower()
    if not sub or not email or identity.get("email_verified") is not True:
        raise GoogleLoginError("Google account has no verified email")

    user = db.scalar(select(User).where(User.google_sub == sub))
    if user:
        return user, False
    user = db.scalar(select(User).where(User.email == email))
    if user:  # existing password account with the same verified email: link it
        if not user.email_verified_at:
            # Nobody ever proved they own this address. It may have been registered by someone else
            # to sit in wait ("pre-account hijacking"): drop that password and sign out all sessions.
            user.password_hash = ""
            user.session_epoch = (user.session_epoch or 0) + 1
        user.google_sub = sub
        user.email_verified_at = user.email_verified_at or now()  # Google just proved they own it
        db.commit()
        return user, False
    user = User(email=email, name=(identity.get("name") or "").strip()[:100], password_hash="", google_sub=sub,
                email_verified_at=now())
    db.add(user)
    db.commit()
    from app.services import alerts
    alerts.signup(email, "Google")
    return user, True
