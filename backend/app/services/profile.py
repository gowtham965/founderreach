"""Turn a resume and/or LinkedIn profile export (PDF or text) into a structured profile.

Users can attach both; each kind replaces only its own earlier upload, and the profile is
built from whatever we hold. We never open LinkedIn pages: the profile URL is only a link
for founders to click.
"""
import io
import re

from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.llm import get_llm
from app.llm import usage
from app.models import Profile, User

EXTRACT_SYSTEM = """You read resumes and build a candidate profile for a job agent that
cold-emails startup founders. Be concrete. Highlights must be specific wins taken from
the resume, keeping any numbers exactly as written. Never invent facts."""

EXTRACT_PROMPT = """{documents}
Combine everything above into one profile. Where documents disagree, prefer the more
detailed or more recent one.
Return JSON:
{{"name": str, "summary": "2-3 sentence background", "skills": [str],
  "highlights": ["concrete achievement with numbers if present"],
  "target_roles": ["roles this person is plausibly aiming for"],
  "links": {{"linkedin": str|null, "github": str|null, "portfolio": str|null}}}}"""


def pdf_to_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


_LINKEDIN_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]{3,100})", re.I)
_SLUG_RE = re.compile(r"^[A-Za-z0-9\-_%]{3,100}$")


def normalize_linkedin(value: str | None) -> str | None:
    """'gowtham-r-gowda', '@gowtham-r-gowda' or any linkedin.com/in/ URL → canonical profile URL."""
    v = (value or "").strip()
    m = _LINKEDIN_RE.search(v)
    slug = m.group(1) if m else v.lstrip("@").strip("/")
    return f"https://www.linkedin.com/in/{slug}" if _SLUG_RE.match(slug) else None


def is_linkedin_export(text: str) -> bool:
    """LinkedIn's "Save to PDF" has the profile URL up top, a Top Skills box and "Page N of M" footers."""
    low = text.lower()
    return "linkedin.com/in/" in low and ("top skills" in low or bool(re.search(r"page \d+ of \d+", low)))


def get_or_create_profile(db: Session, user: User) -> Profile:
    if user.profile is None:
        user.profile = Profile(user_id=user.id, preferences={}, links={}, skills=[], highlights=[], target_roles=[])
        db.add(user.profile)
        db.commit()
    return user.profile


@usage.charged
def ingest_document(db: Session, user: User, text: str) -> tuple[Profile, str]:
    """Store a resume or LinkedIn export (replacing only the earlier one of the same kind) and
    rebuild the profile from everything held. Returns (profile, "resume" | "linkedin")."""
    profile = get_or_create_profile(db, user)
    kind = "linkedin" if is_linkedin_export(text) else "resume"
    if kind == "linkedin":
        profile.linkedin_text = text
    else:
        profile.resume_text = text
    docs = [(label, body) for label, body in (("Resume", profile.resume_text), ("LinkedIn profile", profile.linkedin_text)) if body]
    documents = "\n\n".join(f"{label}:\n---\n{body[:15000]}\n---" for label, body in docs)
    data = get_llm().complete_json(EXTRACT_SYSTEM, EXTRACT_PROMPT.format(documents=documents), task="extract_profile")
    profile.summary = data.get("summary", "")
    profile.skills = data.get("skills", [])[:40]
    profile.highlights = data.get("highlights", [])[:10]
    if not profile.target_roles:
        profile.target_roles = data.get("target_roles", [])[:5]
    links = {k: v for k, v in (data.get("links") or {}).items() if v}
    links.update({k: v for k, v in (profile.links or {}).items() if v})  # what the user told us wins
    found = normalize_linkedin(_LINKEDIN_RE.search(text).group(0)) if _LINKEDIN_RE.search(text) else None
    if found and not links.get("linkedin"):
        links["linkedin"] = found
    elif links.get("linkedin"):
        links["linkedin"] = normalize_linkedin(links["linkedin"]) or links["linkedin"]
    profile.links = links
    if data.get("name") and not user.name:
        user.name = data["name"]
    db.commit()
    return profile, kind


def upload_reply(p: Profile, kind: str) -> str:
    """What we say after reading a resume or LinkedIn PDF (web upload and WhatsApp document alike)."""
    wins = "\n".join(f"• {h}" for h in (p.highlights or [])[:3])
    both = bool(p.resume_text and p.linkedin_text)
    what = "your LinkedIn profile" if kind == "linkedin" else "your resume"
    msg = f"read {what} ✅{' (combined with your ' + ('resume' if kind == 'linkedin' else 'LinkedIn') + ')' if both else ''} {p.summary}\n\n"
    msg += f"Wins I'll lead with:\n{wins}\n\n"
    if not (p.links or {}).get("linkedin"):
        msg += ("Paste your LinkedIn profile link too (open your profile and copy the address). "
                "I'll add it to your emails so founders can look you up.\n\n")
    msg += "And what do you want next? Kind of role, sectors you love or want to avoid, remote or in-office."
    return msg


def document_text(data: bytes, filename: str = "", mime: str = "") -> str:
    """PDF → text; anything else is read as plain text."""
    if filename.lower().endswith(".pdf") or mime == "application/pdf":
        try:
            return pdf_to_text(data)
        except Exception:  # empty, encrypted or broken PDFs: the caller says "couldn't read much text"
            return ""
    return data.decode("utf-8", "ignore")


def update_profile(db: Session, user: User, **fields) -> Profile:
    profile = get_or_create_profile(db, user)
    for key in ("summary", "skills", "highlights", "target_roles", "links"):
        if fields.get(key):
            setattr(profile, key, fields[key])
    if fields.get("linkedin"):
        url = normalize_linkedin(fields["linkedin"])
        if not url:
            raise ValueError("That doesn't look like a LinkedIn profile link. Open your profile and copy the address; "
                             "it has linkedin.com/in/ in it")
        profile.links = {**(profile.links or {}), "linkedin": url}
    if fields.get("preferences"):
        prefs = dict(profile.preferences or {})
        prefs.update({k: v for k, v in fields["preferences"].items() if v not in (None, "", [])})
        profile.preferences = prefs
    db.commit()
    return profile


def profile_brief(profile: Profile | None) -> str:
    if profile is None:
        return "No profile yet."
    p = profile.preferences or {}
    return (
        f"Summary: {profile.summary or '-'}\n"
        f"Skills: {', '.join(profile.skills or []) or '-'}\n"
        f"Highlights: {'; '.join(profile.highlights or []) or '-'}\n"
        f"Target roles: {', '.join(profile.target_roles or []) or '-'}\n"
        f"LinkedIn: {(profile.links or {}).get('linkedin') or '-'}\n"
        f"Preferences: {p or '-'}"
    )
