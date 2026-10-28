"""Email addresses: confidence levels, name patterns, matching an address to a name, and Hunter lookups."""
from __future__ import annotations

import re

from app.config import get_settings
from app.services.contacts import fetch

CONFIDENCE_RANK = {"verified": 5, "public": 4, "likely": 3, "inbox": 2, "guess": 1}
INBOX_NAMES = ("founders", "founder", "hello", "hi", "team", "jobs", "careers", "hiring", "contact", "info")


def has_mx(domain: str) -> bool:
    try:
        import dns.resolver

        return bool(dns.resolver.resolve(domain, "MX", lifetime=4))
    except Exception:
        return False


def split_name(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "", ""
    return parts[0], parts[-1] if len(parts) > 1 else ""


def _ascii(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def pattern_guesses(first: str, last: str, domain: str) -> list[str]:
    f, l = _ascii(first), _ascii(last)
    if not f:
        return []
    guesses = [f"{f}@{domain}"]
    if l:
        guesses += [f"{f}.{l}@{domain}", f"{f[0]}{l}@{domain}", f"{f}{l}@{domain}"]
    return guesses


def email_matches_name(email: str, name: str) -> bool:
    """asha@, ashara@, asha.rao@ (dots are stripped), arao@, raoasha@ all match "Asha Rao"."""
    local = _ascii(email.split("@")[0])
    first, last = (_ascii(x) for x in split_name(name))
    if not first:
        return False
    options = {first}
    if last:
        options |= {first + last, first[0] + last, last + first}
    return local in options



def hunter_find(first: str, last: str, domain: str) -> dict | None:
    key = get_settings().hunter_api_key
    if not key or not first:
        return None
    r = fetch._get("https://api.hunter.io/v2/email-finder",
             params={"domain": domain, "first_name": first, "last_name": last, "api_key": key})
    data = (r.json().get("data") if r is not None else None) or {}
    if not data.get("email"):
        return None
    status = (data.get("verification") or {}).get("status")
    return {"email": data["email"].lower(), "confidence": "verified" if status == "valid" else "likely", "source": "hunter"}


def verify(email: str) -> str:
    """'valid' | 'invalid' | 'accept_all' | 'unknown' (no key → unknown)."""
    key = get_settings().hunter_api_key
    if not key:
        return "unknown"
    r = fetch._get("https://api.hunter.io/v2/email-verifier", params={"email": email, "api_key": key})
    status = ((r.json().get("data") if r is not None else None) or {}).get("status", "unknown")
    return status if status in ("valid", "invalid", "accept_all") else "unknown"
