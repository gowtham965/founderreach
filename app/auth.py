"""Email + password accounts with JWT bearer tokens (stdlib PBKDF2 hashing)."""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

_ITERATIONS = 260_000
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_token(user_id: int, purpose: str = "session", minutes: int = 60 * 24 * 14, epoch: int | None = 0) -> str:
    payload = {
        "sub": str(user_id),
        "purpose": purpose,
        "ep": epoch or 0,  # the account's session version: bumping it signs out every older token
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, get_settings().secret_key, algorithm="HS256")


def session_token(user: User) -> str:
    return create_token(user.id, epoch=user.session_epoch)


def decode_payload(token: str, purpose: str = "session") -> dict:
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc
    if payload.get("purpose") != purpose:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong token type")
    return payload


def decode_token(token: str, purpose: str = "session") -> int:
    return int(decode_payload(token, purpose)["sub"])


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    payload = decode_payload(creds.credentials)
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
    if payload.get("ep", 0) != (user.session_epoch or 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Signed out. Please sign in again")
    touch(db, user)
    return user


def touch(db: Session, user: User) -> None:
    """Remember they're active, at most once an hour (keeps writes rare)."""
    now = datetime.now(timezone.utc)
    last = user.last_seen_at.replace(tzinfo=timezone.utc) if user.last_seen_at and user.last_seen_at.tzinfo is None else user.last_seen_at
    if last is None or now - last > timedelta(hours=1):
        user.last_seen_at = now
        db.commit()


def is_admin(user) -> bool:
    """Listed in ADMIN_EMAILS *and* proven to own that address (confirmed, or Google sign-in).
    Otherwise anyone could sign up with the admin's email first and become admin."""
    admins = {e.strip().lower() for e in get_settings().admin_emails.split(",") if e.strip()}
    return bool(user.email_verified_at) and user.email.lower() in admins
