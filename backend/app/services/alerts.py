"""Instant phone alerts for the admin through ntfy (https://ntfy.sh): new sign-ups, payments, things breaking.

Set NTFY_TOPIC to a long random topic name and subscribe to it in the ntfy app; anyone who knows the name can read
it, so treat it like a password. Emails are partly hidden because messages pass through ntfy's server (the admin
tab shows them in full). Sending happens on a background thread and never raises, so a slow or down ntfy can't
slow down or break a sign-up or a payment.
"""
import logging
import threading

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


def mask(email: str) -> str:
    """priya.sharma@gmail.com -> pr***@gmail.com"""
    local, _, domain = (email or "").partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


def _post(title: str, message: str, tags: str, priority: str) -> None:
    s = get_settings()
    try:
        httpx.post(f"{s.ntfy_server.rstrip('/')}/{s.ntfy_topic}", content=message.encode(), timeout=8,
                   headers={"Title": title, "Tags": tags, "Priority": priority})  # headers stay ASCII; emoji via tags
    except Exception as exc:
        log.warning("ntfy alert failed: %s", exc)


def send(title: str, message: str, tags: str = "", priority: str = "default", wait: bool = False) -> bool:
    """Fire one alert. Returns False when alerts aren't set up."""
    if not get_settings().ntfy_topic:
        return False
    if wait:
        _post(title, message, tags, priority)
    else:
        threading.Thread(target=_post, args=(title, message, tags, priority), daemon=True).start()
    return True


def signup(email: str, via: str) -> None:
    send("New sign-up", f"{mask(email)} joined with {via}.", tags="tada")


def payment(email: str, plan_label: str, amount_inr: int) -> None:
    send("Payment received", f"Rs {amount_inr}: {plan_label}, {mask(email)}", tags="moneybag", priority="high")


def problem(what: str) -> None:
    send("FounderReach needs a look", what, tags="warning", priority="high")


_last_crash: dict[str, float] = {}


def crash(where: str, exc: BaseException) -> None:
    """An unhandled server error. At most one alert per kind of error every 15 minutes."""
    import time
    key = f"{where}:{type(exc).__name__}"
    if time.time() - _last_crash.get(key, 0) < 15 * 60:
        return
    _last_crash[key] = time.time()
    send("Server error", f"{type(exc).__name__} on {where}: {str(exc)[:200]}", tags="rotating_light", priority="high")
