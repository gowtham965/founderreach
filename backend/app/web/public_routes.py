"""No sign-in needed: the web app's page, the landing page's live data, policy pages, favicon and health check."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import db as db_module, pages
from app.db import get_db
from app.services import matching
from app.web.security import client_ip, rate_limited

router = APIRouter()
log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parents[3] / "frontend"  # served at /static


@router.api_route("/", include_in_schema=False, methods=["GET", "HEAD"])
def index():
    v = pages.asset_v()  # versioned links: after a deploy, browsers load the new CSS and JS
    return HTMLResponse((STATIC / "index.html").read_text()
                        .replace('/static/styles.css"', f'/static/styles.css?v={v}"')
                        .replace('/static/app.js"', f'/static/app.js?v={v}"')
                        .replace("</body>", pages.analytics_tag() + "</body>"))


@router.get("/api/public/ip")
def public_ip(request: Request):
    """The address rate limits see for you (checks the proxy setup; it's only your own IP)."""
    return {"ip": client_ip(request)}


@router.get("/api/public/stats")
def public_stats(db: Session = Depends(get_db)):
    return matching.public_stats(db)


@router.get("/api/public/fresh")
def public_fresh(db: Session = Depends(get_db)):
    return matching.public_fresh(db)


@router.get("/api/public/preview")
def public_preview(request: Request, q: str = "", db: Session = Depends(get_db)):
    if rate_limited(client_ip(request)):
        raise HTTPException(429, "Slow down a little and try again in a minute.")
    return matching.public_preview(db, q[:200])


@router.api_route("/terms", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def terms_page():
    return pages.terms()


@router.api_route("/privacy", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def privacy_page():
    return pages.privacy()


@router.api_route("/refunds", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def refunds_page():
    return pages.refunds()


@router.api_route("/contact", include_in_schema=False, response_class=HTMLResponse, methods=["GET", "HEAD"])
def contact_page():
    return pages.contact()


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "brand" / "favicon.ico", media_type="image/x-icon")


@router.api_route("/healthz", include_in_schema=False, methods=["GET", "HEAD"])
def health():
    """For Render and the outside uptime monitor: up only if the database answers too."""
    try:
        with db_module.SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        log.exception("health check: database unavailable")
        return PlainTextResponse("database unavailable", status_code=503)
    return HTMLResponse("ok")
