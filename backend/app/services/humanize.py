"""Humanize pass for outgoing emails, built on the vendored humanize-writing skill.

draft → rewrite with the skill's rules → lint for leftover tells → (one retry naming them)

The skill was written for long-form editing, so a few of its instructions are overridden
for 100-word cold emails: no "Changes" table, no tangents or opinions that pad length,
and never add facts the candidate didn't give us.
"""
import logging
import re
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.llm import get_llm

log = logging.getLogger(__name__)
SKILL_DIR = Path(__file__).resolve().parents[2] / "prompts" / "humanize"

EMAIL_RULES = """You are editing a cold email (or follow-up) from a job seeker to a startup founder.
Apply the editing passes below, with these overrides for email:
- Return only the rewritten email. No "Changes" table, no commentary.
- Keep it as short or shorter than the input. Cold emails stay under 110 words, follow-ups under 50.
- Keep every fact, number, name, link and the single ask exactly. Never add a fact, metric,
  company detail or experience that isn't in the input.
- "Human texture" here means plain, direct, conversational sentences with varied length and
  contractions. Do not add tangents, opinions about the industry, slang or jokes.
- No em dashes, no emojis, no bold, no lists. Plain text.
- Keep the sign-off name as is."""

# --- linter (word lists from ai-tells.md) ---
TIER1 = ["delve", "tapestry", "paradigm shift", "fundamental shift", "game-changer", "game changer", "leverage",
         "harness", "navigate", "navigating", "realm", "embark", "journey", "myriad", "plethora", "multifaceted",
         "groundbreaking", "revolutionize", "synergy", "synergies", "resonate", "resonates", "streamline", "testament",
         "landscape", "ecosystem", "enduring"]
TIER2 = ["robust", "seamless", "cutting-edge", "innovative", "comprehensive", "pivotal", "nuanced", "compelling",
         "transformative", "bolster", "underscore", "evolving", "fostering", "imperative", "intricate", "overarching",
         "unprecedented", "vibrant", "profound", "renowned", "stunning", "showcasing", "exemplifies", "garner", "valuable"]
PHRASES = [
    r"it'?s (worth|important) (noting|to note|mentioning)", r"i hope (this|you|the) .{0,20}(finds|well|helps)",
    r"in today'?s", r"at its core", r"at the end of the day", r"in order to", r"due to the fact",
    r"let me know if you'?d like", r"great question", r"\bcertainly\b", r"\babsolutely\b", r"that being said",
    r"\bmoreover\b", r"\bfurthermore\b", r"\badditionally\b", r"\bin conclusion\b", r"passionate about",
    r"results[- ]driven", r"\bkeen interest\b", r"aligns? (perfectly )?with my", r"at your earliest convenience",
    r"not (just|only) .{1,40}(it'?s|but)", r"\b(highlighting|underscoring|showcasing|emphasizing|fostering|reflecting) (the|how|its|my)",
    r"would welcome the opportunity", r"thrive in fast[- ]paced", r"proven track record",
]
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def _word_re(w: str) -> str:
    """Single words match their inflections (leverage → leveraging, leveraged)."""
    return rf"\b{re.escape(w)}\b" if " " in w or "-" in w else rf"\b{re.escape(w.rstrip('e'))}\w*"


def find_tells(text: str) -> list[str]:
    low = text.lower()
    tells = [w for w in TIER1 if re.search(_word_re(w), low)]
    t2 = [w for w in TIER2 if re.search(_word_re(w), low)]
    if len(t2) >= 2:  # stricter than the skill's 3+ because emails are ~100 words
        tells += t2
    for p in PHRASES:
        m = re.search(p, low)
        if m:
            tells.append(m.group(0))
    if "—" in text or " -- " in text:
        tells.append("em dash")
    if _EMOJI.search(text):
        tells.append("emoji")
    return tells


@lru_cache
def _skill_prompt() -> str:
    try:
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        tells = (SKILL_DIR / "ai-tells.md").read_text(encoding="utf-8")
    except OSError:
        log.warning("humanize skill files missing; using built-in rules only")
        return EMAIL_RULES
    start, end = skill.find("## The Editing Process"), skill.find("## Output Format")
    passes = skill[start:end] if start != -1 and end > start else skill
    return f"{EMAIL_RULES}\n\n{passes}\n\n{tells}"


def humanize(subject: str, body: str, kind: str = "cold email") -> dict:
    """Returns {"subject", "body", "tells_before", "tells_after"}. Falls back to the input on any failure."""
    mode = get_settings().humanize_mode
    before = find_tells(f"{subject}\n{body}")
    result = {"subject": subject, "body": body, "tells_before": before, "tells_after": before}
    if mode == "off" or (mode == "when_tells" and not before):
        return result
    llm = get_llm()
    cur_subject, cur_body, tells = subject, body, before
    for attempt in range(2):
        named = f"\nThese AI tells were detected and must go: {', '.join(tells)}\n" if tells else ""
        prompt = (f"Rewrite this {kind}.{named}\nSubject: {cur_subject}\n\n{cur_body}\n\n"
                  'Return JSON: {"subject": str, "body": str}')
        try:
            data = llm.complete_json(_skill_prompt(), prompt, task="humanize")
        except Exception as exc:
            log.warning("humanize pass failed: %s", exc)
            break
        new_body = (data or {}).get("body") or ""
        if not new_body.strip() or len(new_body.split()) > len(cur_body.split()) * 1.3 + 10:
            break  # empty or ballooned rewrite: keep what we have
        cur_subject = ((data or {}).get("subject") or cur_subject).strip()
        cur_body = new_body.strip()
        tells = find_tells(f"{cur_subject}\n{cur_body}")
        if not tells:
            break
    result.update(subject=cur_subject, body=cur_body, tells_after=tells)
    return result
