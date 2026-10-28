"""Find where a startup lists its roles: job-board links on its homepage and careers pages."""
import re
from urllib.parse import urljoin, urlparse

from app.services.jobs import polite

BOARD_LINKS = {
    "greenhouse": re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=|v1/boards/)?([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
    "workable": re.compile(r"apply\.workable\.com/(?!api\b|j/)([A-Za-z0-9_-]+)"),
    "smartrecruiters": re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    "recruitee": re.compile(r"\b([a-z0-9-]+)\.recruitee\.com"),
    "breezy": re.compile(r"\b([a-z0-9-]+)\.breezy\.hr"),
}
NOT_SLUGS = {"embed", "api", "v1", "www", "app", "careers", "jobs", "static", "assets"}
CAREER_PATHS = ("/careers", "/jobs", "/join-us", "/work-with-us")
_HREF = re.compile(r"""href=["']([^"'#?]+)""", re.I)
_CAREERISH = re.compile(r"career|jobs|join|hiring|work-with-us|open-roles", re.I)


def boards_in(html: str) -> dict[str, str]:
    """{board: slug} for every job-board link in the page."""
    found = {}
    for board, rx in BOARD_LINKS.items():
        for slug in rx.findall(html):
            if slug.lower() not in NOT_SLUGS:
                found.setdefault(board, slug)
                break
    return found


def careers_pages(domain: str) -> list[tuple[str, str]]:
    """[(url, html)] for the homepage, the usual careers paths, and a careers link on the homepage (same site only)."""
    base = f"https://{domain}"
    pages, seen = [], set()

    def fetch(url: str) -> str | None:
        url = url.split("#")[0].rstrip("/") or base
        if url in seen or len(seen) >= 6:
            return None
        seen.add(url)
        r = polite.get(url, check_robots=True)
        if r is None:
            return None
        html = r.text[:600_000]
        pages.append((str(r.url), html))
        return html

    home = fetch(base)
    for href in [h for h in _HREF.findall(home or "") if _CAREERISH.search(h)][:2]:  # their own careers link first
        url = urljoin(base + "/", href)
        host = (urlparse(url).hostname or "").lower()
        if host == domain or host.endswith("." + domain):
            fetch(url)
    for path in CAREER_PATHS:  # then the usual places
        fetch(base + path)
    return pages
