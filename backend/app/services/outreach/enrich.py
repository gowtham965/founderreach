"""Founder contacts, open roles and GitHub for a startup: looked up once and shared by every user for two weeks."""
import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import Startup, aware, now
from app.services import contacts as contacts_svc

log = logging.getLogger(__name__)


ENRICH_TTL = timedelta(days=14)


def enrich_startup(db: Session, s: Startup, force: bool = False) -> Startup:
    """Run the contact waterfall once per startup (cached for two weeks; shared by all users)."""
    if not force and s.enriched_at and aware(s.enriched_at) > now() - ENRICH_TTL:
        return s
    try:
        data = contacts_svc.enrich(s.name, s.domain, s.founders, s.source_url if s.source == "yc" else "")
    except Exception as exc:
        log.warning("enrichment failed for %s: %s", s.name, exc)
        return s
    s.founders = data["founders"] or s.founders or []
    s.contacts, s.jobs, s.github_org, s.enriched_at = data["contacts"], data["jobs"], data["github"], now()
    db.commit()
    return s


def _roles_line(s: Startup) -> str:
    jobs = s.jobs or []
    if not jobs:
        return "Open roles: none listed publicly"
    return "Open roles: " + "; ".join(f"{j['title']} ({j.get('location') or 'location n/a'})" for j in jobs[:6])
