"""The waterfall: run every step in order of cost and trust, and rank what was found (see the package docstring)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.contacts import emails, jobs, names, website
from app.services.contacts.emails import CONFIDENCE_RANK, INBOX_NAMES


def enrich(name: str, domain: str | None, known_founders: list[dict] | None = None, yc_url: str = "") -> dict:
    """Run the full waterfall. Returns {"founders": [...], "contacts": [...], "jobs": [...], "github": str|None}.

    contacts are sorted best first; each is {email, name, title, confidence, source}.
    """
    result = {"founders": [], "contacts": [], "jobs": [], "github": None,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    if not domain:
        return result

    # 1. founder names
    founders: dict[str, dict] = {}
    for f in (known_founders or []):
        if f.get("name"):
            founders.setdefault(f["name"], {"title": "Founder", "source": "news", **f})
    for f in names.yc_founders(name, yc_url, domain):
        founders.setdefault(f["name"], f).update({k: v for k, v in f.items() if v and k not in founders[f["name"]]})
    site = website.crawl_site(domain)
    if not founders:
        for f in names.names_from_site_text(site["team_text"], name):
            founders.setdefault(f["name"], f)
    for f in founders.values():
        f.setdefault("linkedin_search", names.linkedin_search_url(f["name"], name))
    result["founders"] = list(founders.values())
    result["github"] = site["github"]

    contacts: dict[str, dict] = {}

    def add(email, confidence, source, person=None):
        email = email.lower()
        cur = contacts.get(email)
        if cur and CONFIDENCE_RANK[cur["confidence"]] >= CONFIDENCE_RANK[confidence]:
            return
        contacts[email] = {"email": email, "confidence": confidence, "source": source,
                           "name": (person or {}).get("name", ""), "title": (person or {}).get("title", "")}

    # 2. published company-domain emails (website + GitHub commits)
    published = {e: "" for e in site["emails"]}
    if site["github"]:
        published.update({e: n for e, n in website.github_emails(site["github"], domain).items() if e not in published})
    for email, author in published.items():
        person = next((f for f in founders.values() if emails.email_matches_name(email, f["name"])), None)
        if person:
            add(email, "public", "website" if email in site["emails"] else "github", person)
        elif email.split("@")[0] in INBOX_NAMES:
            add(email, "inbox", "website")

    lead = next(iter(founders.values()), None)
    have_founder_email = any(c["name"] for c in contacts.values())

    # 3. paid lookup for the lead founder
    if lead and not have_founder_email:
        first, last = emails.split_name(lead["name"])
        hit = emails.hunter_find(first, last, domain)
        if hit:
            add(hit["email"], hit["confidence"], "hunter", lead)
            have_founder_email = True

    # 4. pattern + verify (max 3 verifier calls)
    if lead and not have_founder_email and emails.has_mx(domain):
        first, last = emails.split_name(lead["name"])
        tries = 0
        for guess in emails.pattern_guesses(first, last, domain):
            status = emails.verify(guess) if tries < 3 else "unknown"
            tries += status != "unknown"
            if status == "valid":
                add(guess, "verified", "pattern", lead)
                break
            if status == "accept_all":  # server takes everything; the plain first-name pattern is the best bet
                add(emails.pattern_guesses(first, last, domain)[0], "likely", "pattern", lead)
                break
            if status == "unknown":  # no verifier: keep the most common startup pattern as a guess
                add(guess, "guess", "pattern", lead)
                break

    # 5. shared inbox fallback
    if not any(c["confidence"] == "inbox" for c in contacts.values()) and emails.has_mx(domain):
        add(f"founders@{domain}", "guess", "inbox-guess", {"name": "", "title": "Founders inbox"})

    result["contacts"] = sorted(contacts.values(), key=lambda c: (-CONFIDENCE_RANK[c["confidence"]], not c["name"]))
    result["jobs"] = jobs.open_roles(site["ats"], domain)
    return result


def best_contact(contacts: list[dict]) -> dict | None:
    return contacts[0] if contacts else None
