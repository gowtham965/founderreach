"""Follow-up reminders: when one is due, write the follow-up and tell the user in their chat. Nothing is sent."""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import get_llm
from app.llm import usage
from app.models import Outreach, User, now
from app.services.humanize import humanize
from app.services.outreach.common import compose_links, notify
from app.services.outreach.prompts import followup_system
from app.services.profile import profile_brief

log = logging.getLogger(__name__)


def _followup_days() -> list[int]:
    return [int(d) for d in get_settings().followup_days.split(",") if d.strip()]


def prepare_due_followups(db: Session) -> int:
    """Follow-ups that are due get a fresh draft and a reminder in the user's chat. Nothing is sent."""
    due = db.scalars(select(Outreach).where(Outreach.status == "sent", Outreach.followups_enabled.is_(True),
                                            Outreach.followup_draft.is_(None), Outreach.next_followup_at <= now()))
    count = 0
    for o in list(due):
        try:
            if o.followups_sent >= len(_followup_days()):
                o.next_followup_at = None
                db.commit()
                continue
            user = db.get(User, o.user_id)
            from app.services import billing
            if not billing.has_access(user):  # stays due: drafted once they get a pass
                continue
            with usage.for_user(user):  # their follow-up: charged to them
                data = get_llm().complete_json(
                    followup_system(),
                    f"Candidate:\n{profile_brief(user.profile)}\nName: {user.name}\n\nOriginal email:\n{o.body}\n\n"
                    f"Startup: {o.match.startup.name}: {o.match.startup.one_liner}\nReturn JSON: {{\"body\": str}}",
                    task="followup",
                )
                o.followup_draft = humanize("", data["body"], kind="follow-up")["body"]
            db.commit()
            who = o.to_name or o.match.startup.name
            link = compose_links(o.to_email, "Re: " + o.subject.removeprefix("Re: "), o.followup_draft)["gmail"]
            notify(db, user.id, f"⏰ No reply from {who} at {o.match.startup.name} yet? Here's a short follow-up (#{o.match_id}):\n\n"
                                f"{o.followup_draft}\n\nBest sent as a reply to your first email so it stays in one thread. "
                                f"Or open it ready to send: {link}\n\n"
                                "Tell me when it's sent, or if they already replied and I'll stop reminding you.")
            count += 1
        except Exception:
            log.exception("follow-up draft failed for outreach %s", o.id)
            db.rollback()
    return count
