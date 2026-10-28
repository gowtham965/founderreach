"""Open roles at startups, from public job-board APIs and careers pages (never LinkedIn, Naukri or other sites
whose terms forbid scraping). See refresh.py for how roles open and close, and polite.py for how we fetch."""
from app.services.jobs.refresh import refresh_startup, run, same_company, save, scan  # noqa: F401
