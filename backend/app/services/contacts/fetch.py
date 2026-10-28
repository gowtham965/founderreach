"""Fetching public pages for the contact waterfall: one shared HTTP client, plus small text and domain helpers."""
from __future__ import annotations

import html as html_lib
import logging
import re
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_client: httpx.Client | None = None


def client() -> httpx.Client:
    """Shared HTTP client (tests swap this for one with a mock transport)."""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=12, follow_redirects=True,
                               headers={"User-Agent": "FounderReach/1.0 (+job search assistant)"})
    return _client


def _get(url: str, **kw) -> httpx.Response | None:
    try:
        r = client().get(url, **kw)
        return r if r.status_code == 200 else None
    except httpx.HTTPError as exc:
        log.debug("GET %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------- helpers
def normalize_domain(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    s = url_or_domain.strip().lower()
    if "://" not in s:
        s = "http://" + s
    host = (urlparse(s).hostname or "").removeprefix("www.")
    return host if "." in host else None



def _text(page_html: str) -> str:
    page_html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", page_html)
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", page_html))).strip()
