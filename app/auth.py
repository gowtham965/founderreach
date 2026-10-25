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


def create_token(user_id: int, purpose: str = "session", minutes: int = 60 * 24 * 14) -> str:
    payload = {
        "sub": str(user_id),
        "purpose": purpose,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, get_settings().secret_key, algorithm="HS256")


def decode_token(token: str, purpose: str = "session") -> int:
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc
    if payload.get("purpose") != purpose:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong token type")
    return int(payload["sub"])


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    user = db.get(User, decode_token(creds.credentials))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
    return user
