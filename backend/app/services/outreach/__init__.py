"""Drafts, the user's own sending, follow-up reminders and replies.

FounderReach never sends email. It writes the draft; the user sends it from their own
email (compose links prefill Gmail or their mail app) and tells us "I sent it". Follow-ups
are reminders with a fresh draft, and stop when the user says the founder replied.

Hard rules enforced here (not in the prompt):
  * founder emails and drafts need a confirmed email (stops throwaway accounts harvesting founders)
  * a per-user daily cap on drafts (MAX_DRAFTS_PER_DAY) keeps AI costs bounded
"""
from app.services.outreach.common import (OutreachError, _owned_match, _use_draft_quota, compose_links,  # noqa: F401
                                          notify, require_confirmed, with_linkedin)
from app.services.outreach.drafts import draft, edit_draft, make_dm, set_recipient, write_dm  # noqa: F401
from app.services.outreach.enrich import ENRICH_TTL, _roles_line, enrich_startup  # noqa: F401
from app.services.outreach.followups import _followup_days, prepare_due_followups  # noqa: F401
from app.services.outreach.prompts import DRAFT_PROMPT, draft_system, followup_system, style_guide  # noqa: F401
from app.services.outreach.tracking import (cancel_followups, mark_followup_sent, mark_replied, mark_sent,  # noqa: F401
                                            skip, stats)
