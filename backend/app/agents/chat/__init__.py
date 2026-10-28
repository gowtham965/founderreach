"""The chat agent: one tool-using loop per user message.

The model decides what to do; the tools (and the outreach service under them)
enforce the rules. FounderReach never sends email: the user sends drafts from their own
inbox. Marking something sent or replied needs the user's own latest message to say so,
so a confused model can't rewrite their pipeline.
"""
from app.agents.chat.cards import card_refs, rebuild_cards  # noqa: F401
from app.agents.chat.loop import _history, over_daily_chat_cap, respond  # noqa: F401
from app.agents.chat.prompts import AI_UNAVAILABLE, SYSTEM, WHATSAPP_NOTE  # noqa: F401
from app.agents.chat.tools import TOOLS, _run_tool, user_said_so  # noqa: F401
