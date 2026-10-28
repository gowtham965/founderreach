"""The writing instructions: the house email style (prompts/email_style.md), the draft prompt, the DM note."""
from pathlib import Path

STYLE_FILE = Path(__file__).resolve().parents[3] / "prompts" / "email_style.md"  # backend/prompts/


def style_guide() -> str:
    """Read fresh each time so edits to the style file apply without a restart."""
    try:
        return STYLE_FILE.read_text(encoding="utf-8")
    except OSError:
        return "Write short, specific, human emails. Use only facts given to you."


def draft_system() -> str:
    return "You write cold emails from a job seeker to a startup founder. Follow this style guide exactly.\n\n" + style_guide()


def followup_system() -> str:
    return ("Write a short follow-up in an existing email thread to a startup founder who has not replied. "
            "Follow the style guide's follow-up rules exactly.\n\n" + style_guide())

DRAFT_PROMPT = """Candidate:
{profile}
Candidate name: {name}

Startup: {startup} ({domain})
What they do: {one_liner}
Why now: {signal}
Why it fits: {reason}
{roles}
{research}
(If one open role clearly fits the candidate, name it in one short clause. Otherwise don't mention roles.)
Recipient: {recipient}
{current}{instructions}
Return JSON: {{"subject": str, "body": str}}"""


DM_SYSTEM = """Write a LinkedIn connection note (or X DM) from a job seeker to a startup founder.
Hard limit 280 characters. One specific line about the startup, one concrete win of the
candidate's with its number, one small ask. No greeting fluff, no emojis, no hashtags, no
em dashes. Use only facts given. Sign with the first name."""
