"""Security headers, the visitor's real IP, and the in-memory rate limits shared by the routers."""
import time

from fastapi import HTTPException, Request

from app.auth import is_admin
from app.config import get_settings
from app.models import User
from app.services import alerts

# Content Security Policy: only our own scripts; styles and fonts from us and Google Fonts;
# never embeddable in another site's frame.
CSP = "; ".join([
    "default-src 'self'",
    # Cloudflare Web Analytics (no cookies); Razorpay's checkout popup script
    "script-src 'self' https://static.cloudflareinsights.com https://checkout.razorpay.com https://cdn.razorpay.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",  # inline style attributes are used
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://cdn.razorpay.com",
    "connect-src 'self' https://cloudflareinsights.com https://api.razorpay.com https://lumberjack.razorpay.com",
    "frame-src https://api.razorpay.com https://checkout.razorpay.com",  # the popup runs in Razorpay's iframe
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


async def security_headers(request: Request, call_next):
    """Middleware: security headers on every response; an unhandled crash pings the admin's phone."""
    try:
        response = await call_next(request)
    except Exception as exc:  # tell the admin's phone, then let it reach the logs and Sentry
        route = request.scope.get("route")  # "/api/matches/{match_id}/draft", not one alert per match id
        alerts.crash(f"{request.method} {getattr(route, 'path', request.url.path)}", exc)
        raise
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if get_settings().base_url.startswith("https://"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def client_ip(request: Request) -> str:
    """The visitor's IP. Each proxy in front of us appends the address it saw to X-Forwarded-For;
    anything further left was written by the visitor and can be faked. TRUSTED_PROXY_HOPS = how many
    proxies append (Render: 2, Cloudflare then its load balancer). Check it with GET /api/public/ip."""
    hops = get_settings().trusted_proxy_hops
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if hops and len(forwarded) >= hops:
        return forwarded[-hops]
    return request.client.host if request.client else "?"


def require_admin(user: User, message: str = "Admins only") -> None:
    """Admin routes: listed in ADMIN_EMAILS and proven to own that address. Everyone else gets 403."""
    if not is_admin(user):
        raise HTTPException(403, message)


# ---------------------------------------------------------------- sign-up limit
_SIGNUPS: dict[str, list[float]] = {}


def too_many_signups(ip: str, per_hour: int = 5) -> bool:
    """Accounts created from this IP in the last hour (failed attempts, like a typo, don't count)."""
    now_ts = time.monotonic()
    recent = [t for t in _SIGNUPS.get(ip, []) if now_ts - t < 3600]
    _SIGNUPS[ip] = recent
    return len(recent) >= per_hour


def count_signup(ip: str) -> None:
    if len(_SIGNUPS) > 20000:  # keep memory bounded
        _SIGNUPS.clear()
    _SIGNUPS.setdefault(ip, []).append(time.monotonic())


# ---------------------------------------------------------------- sign-in limit
# Failed sign-ins in 15 minutes: 5 per email from one IP (so a stranger can't lock you out
# from your own network), 50 per email overall (guessing spread across many IPs), 20 per IP.
_FAILS: dict[str, list[float]] = {}
_FAIL_WINDOW, _MAX_PER_EMAIL_IP, _MAX_PER_EMAIL, _MAX_PER_IP = 15 * 60, 5, 50, 20


def _recent_fails(key: str) -> list[float]:
    cutoff = time.monotonic() - _FAIL_WINDOW
    fails = [t for t in _FAILS.get(key, []) if t > cutoff]
    if fails:
        _FAILS[key] = fails
    else:
        _FAILS.pop(key, None)
    return fails


def login_blocked(email: str, ip: str) -> int:
    """Seconds to wait, or 0 if this sign-in may be attempted."""
    for key, limit in ((f"ei:{email}|{ip}", _MAX_PER_EMAIL_IP), (f"e:{email}", _MAX_PER_EMAIL), (f"i:{ip}", _MAX_PER_IP)):
        fails = _recent_fails(key)
        if len(fails) >= limit:
            return int(fails[0] + _FAIL_WINDOW - time.monotonic()) + 1
    return 0


def record_fail(email: str, ip: str) -> None:
    if len(_FAILS) > 20000:  # keep memory bounded
        _FAILS.clear()
    for key in (f"ei:{email}|{ip}", f"e:{email}", f"i:{ip}"):
        _FAILS.setdefault(key, []).append(time.monotonic())


def clear_fails(email: str, ip: str) -> None:
    """A correct password clears this IP's count for the account."""
    _FAILS.pop(f"ei:{email}|{ip}", None)


# ---------------------------------------------------------------- public search limit
_preview_hits: dict[str, list[float]] = {}


def rate_limited(ip: str, per_minute: int = 30) -> bool:
    now_ts = time.monotonic()
    hits = [t for t in _preview_hits.get(ip, []) if now_ts - t < 60]
    hits.append(now_ts)
    _preview_hits[ip] = hits
    if len(_preview_hits) > 5000:  # keep memory bounded
        _preview_hits.clear()
    return len(hits) > per_minute
