"""A careful fetcher for job data: honest User-Agent, robots.txt respected on startups' own sites, at most one
request a second to any one host, short timeouts. Job-board APIs are public endpoints made for this."""
import logging
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)
USER_AGENT = "FounderReachBot/1.0 (+https://founderreach.in/contact)"
MIN_GAP = 1.0  # seconds between requests to the same host

_client: httpx.Client | None = None
_last_hit: dict[str, float] = {}
_robots: dict[str, RobotFileParser | None] = {}
_lock = threading.Lock()


def client() -> httpx.Client:
    """Shared client (tests swap it for one with a mock transport)."""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    return _client


def _wait_for(host: str) -> None:
    with _lock:
        gap = MIN_GAP - (time.monotonic() - _last_hit.get(host, 0))
        _last_hit[host] = time.monotonic() + max(gap, 0)
    if gap > 0:
        time.sleep(gap)


def allowed(url: str) -> bool:
    """robots.txt says we may fetch this page (unknown or unreachable robots.txt counts as yes)."""
    parts = urlparse(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = RobotFileParser()
        try:
            r = client().get(f"{root}/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            _robots[root] = rp
        except httpx.HTTPError:
            _robots[root] = None
        if len(_robots) > 5000:
            _robots.clear()
    rp = _robots.get(root)
    return rp is None or rp.can_fetch(USER_AGENT, url)


def get(url: str, *, check_robots: bool = False, **kw) -> httpx.Response | None:
    """GET, politely. None when blocked by robots.txt, unreachable, or not a 200."""
    if check_robots and not allowed(url):
        log.info("robots.txt disallows %s", url)
        return None
    _wait_for(urlparse(url).netloc)
    try:
        r = client().get(url, **kw)
    except httpx.HTTPError as exc:
        log.debug("GET %s failed: %s", url, exc)
        return None
    return r if r.status_code == 200 else None


def get_json(url: str, **kw):
    """Parsed JSON from a 200 response; None on any failure (so a down board is never read as 'no jobs')."""
    r = get(url, **kw)
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        return None
