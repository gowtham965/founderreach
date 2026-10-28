"""The FounderReach web app: creates the FastAPI app, adds the security middleware, serves the frontend
and plugs in the routers. Each area of the site has its own router in app/web/.

Run:  uvicorn app.main:app --reload
"""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app import scheduler
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Startup
from app.services import discovery
from app.web import (account_routes, admin_routes, auth_routes, match_routes, pay_routes, public_routes,
                     startup_routes, whatsapp_routes)
from app.web.public_routes import STATIC
from app.web.security import _FAILS, _SIGNUPS, _preview_hits, client_ip, security_headers  # noqa: F401 (tests use these)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    if s.secret_key == "change-me-in-production" and s.llm_provider != "fake":
        raise RuntimeError("Set SECRET_KEY in .env (run.sh generates one) before running with real users")
    init_db()
    scheduler.start()
    if s.scheduler_enabled:
        _fill_empty_pool()
    yield
    scheduler.stop()


def _fill_empty_pool() -> None:
    """A brand-new server has no startups until the 7am job; fetch them now, in the background."""
    with SessionLocal() as db:
        if db.scalar(select(func.count(Startup.id))):
            return

    def work():
        with SessionLocal() as db:
            logging.getLogger(__name__).info("startup pool empty: running discovery now (%d added)",
                                             discovery.run_discovery(db))

    threading.Thread(target=work, daemon=True).start()


if get_settings().sentry_dsn:  # error tracking: every crash with its stack trace, but no personal data
    import sentry_sdk

    sentry_sdk.init(
        dsn=get_settings().sentry_dsn,
        environment="production" if get_settings().base_url.startswith("https://") else "development",
        send_default_pii=False,          # no IPs, cookies or user details
        max_request_body_size="never",   # never request bodies (sign-up passwords, resumes)
        traces_sample_rate=0.0,          # errors only, no performance tracing
    )

app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.middleware("http")(security_headers)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
for module in (public_routes, auth_routes, account_routes, match_routes, pay_routes, admin_routes, startup_routes,
               whatsapp_routes):
    app.include_router(module.router)
