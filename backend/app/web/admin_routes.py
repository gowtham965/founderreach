"""Admin tools (ADMIN_EMAILS only): overview numbers, refresh the startup pool, ops report, backups, test alert."""
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import scheduler
from app.auth import current_user
from app.db import get_db
from app.models import ChatMessage, User
from app.services import admin as admin_svc, alerts, backup, ops
from app.web.security import require_admin

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/api/admin/overview")
def admin_overview(q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Everything the Admin tab's overview shows."""
    require_admin(user)
    return admin_svc.overview(db, q[:100])


@router.post("/api/admin/discover")
def run_discovery_now(user: User = Depends(current_user)):
    """Kick off a discovery run in the background (each run can spend AI credits)."""
    require_admin(user, "Only the FounderReach admin can refresh the startup pool")
    threading.Thread(target=scheduler.job_discovery, daemon=True).start()  # recorded like the 7am run
    return {"ok": True, "message": "Discovery started; new startups appear in a minute or two."}


@router.post("/api/admin/ops-report")
def ops_report_now(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Today's ops report, posted to the admin's chat."""
    require_admin(user, "Only the FounderReach admin can see the ops report")
    report = ops.build(db)
    db.add(ChatMessage(user_id=user.id, role="assistant", content=report))
    db.commit()
    return {"report": report}


@router.post("/api/admin/test-alert")
def admin_test_alert(user: User = Depends(current_user)):
    """Send a test notification to the admin's phone (ntfy)."""
    require_admin(user)
    if not alerts.send("FounderReach test alert", "Alerts work. You'll get one for each sign-up and payment.", tags="bell", wait=True):
        raise HTTPException(400, "Alerts aren't set up: add NTFY_TOPIC in Render")
    return {"ok": True}


@router.get("/api/admin/backup")
def download_backup(user: User = Depends(current_user)):
    """A fresh database backup, downloaded: the copy that leaves the server."""
    require_admin(user, "Only the FounderReach admin can download backups")
    if backup.db_path() is None:
        raise HTTPException(400, "Backups here are for SQLite databases")
    try:
        made = backup.run()
    except Exception as exc:
        log.exception("backup for download failed")
        raise HTTPException(500, "Couldn't make a backup right now. Check the logs.") from exc
    scheduler.record("backup", True, {**made, "by": "download"})
    return FileResponse(backup.backup_dir() / made["file"], media_type="application/gzip", filename=made["file"],
                        headers={"Cache-Control": "no-store"})
