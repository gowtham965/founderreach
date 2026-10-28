"""Admin → Startups: browse, add, edit, hide, look up again, and CSV export/import for the startup database."""
import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import SessionLocal, get_db
from app.models import Startup, User
from app.services import outreach, startups_admin
from app.web.security import require_admin

router = APIRouter()
log = logging.getLogger(__name__)


class StartupIn(BaseModel):
    name: str | None = Field(None, max_length=255)
    domain: str | None = Field(None, max_length=255)
    one_liner: str | None = Field(None, max_length=2000)
    stage: str | None = Field(None, max_length=64)
    location: str | None = Field(None, max_length=255)
    signal: str | None = Field(None, max_length=2000)
    tags: list[str] | None = None
    source_url: str | None = Field(None, max_length=2000)
    founder_name: str | None = Field(None, max_length=120)
    founder_title: str | None = Field(None, max_length=80)
    founder_email: str | None = Field(None, max_length=255)
    hidden: bool | None = None
    hidden_reason: str | None = Field(None, max_length=255)


class RecheckIn(BaseModel):
    issue: str = Field(pattern="^(no_website|no_founder|no_founder_email|not_checked)$")


def _startup_act(fn):
    try:
        return fn()
    except startups_admin.StartupError as exc:
        raise HTTPException(400, str(exc)) from exc


def recheck(ids: list[int]) -> None:
    """Look up founders, emails and research again (background; uses Hunter lookups when nothing is published)."""
    from app.agents import research
    for sid in ids:
        with SessionLocal() as db:
            s = db.get(Startup, sid)
            if s is None:
                continue
            try:
                outreach.enrich_startup(db, s, force=True)
                research.research_startup(db, s, force=True)
            except Exception:
                log.exception("re-check failed for startup %s", sid)


@router.get("/api/admin/startups")
def admin_startups(q: str = "", stage: str = "", city: str = "", source: str = "", focus: str = "", issue: str = "",
                   hidden: str = "", sort: str = "new", page: int = 1, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    require_admin(user)
    return startups_admin.listing(db, q[:100], stage, city[:60], source, focus, issue, hidden, sort, page)


@router.post("/api/admin/startups")
def admin_add_startup(body: StartupIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    return startups_admin.row(_startup_act(lambda: startups_admin.create(db, body.model_dump(exclude_none=True))))


@router.patch("/api/admin/startups/{startup_id}")
def admin_edit_startup(startup_id: int, body: StartupIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    return startups_admin.row(_startup_act(lambda: startups_admin.update(db, startup_id, body.model_dump(exclude_unset=True))))


@router.post("/api/admin/startups/{startup_id}/recheck")
def admin_recheck_startup(startup_id: int, user: User = Depends(current_user)):
    require_admin(user)
    threading.Thread(target=recheck, args=([startup_id],), daemon=True).start()
    return {"ok": True, "message": "Checking again: founders, emails and research update in a minute."}


@router.post("/api/admin/startups/recheck")
def admin_recheck_many(body: RecheckIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Re-check up to 20 in-focus startups with this gap: each can use a Hunter lookup, and the free plan is small."""
    require_admin(user)
    ids = [r["id"] for r in startups_admin.listing(db, issue=body.issue, focus="yes", per_page=20)["rows"]]
    threading.Thread(target=recheck, args=(ids,), daemon=True).start()
    return {"ok": True, "count": len(ids), "message": f"Re-checking {len(ids)} startups in the background (a few minutes)."}


@router.get("/api/admin/startups.csv")
def admin_export_startups(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    name = f"founderreach-startups-{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    return Response(startups_admin.export_csv(db), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/api/admin/startups/import")
async def admin_import_startups(file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    data = await file.read()
    if len(data) > 5_000_000:
        raise HTTPException(413, "CSV must be under 5 MB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return _startup_act(lambda: startups_admin.import_csv(db, text))
