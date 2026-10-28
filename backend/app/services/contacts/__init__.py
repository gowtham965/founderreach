"""Find a founder's email (and other ways to reach them) for a startup.

Waterfall, cheapest and most trustworthy first. Every step uses public data through
allowed routes; nothing logs in to or scrapes LinkedIn.

  1. Founder names     known names (news) + YC company page + the startup's own About/Team page
  2. Published emails  company-domain addresses on the startup's website or in its public
                       GitHub commits (GitHub API). Only @company addresses, never personal ones.
  3. Paid lookup       Hunter Email Finder, 1 credit, only if a key is set and steps 1-2 left a gap
  4. Pattern + verify  firstname@ / first.last@ / flast@, checked with Hunter Email Verifier if
                       a key is set (0.5 credit each, at most 3 tries)
  5. Shared inbox      founders@ / hello@ / jobs@ published on the site, else founders@ as a guess
  +  Open roles        public Ashby / Lever / Greenhouse job APIs (apply link to pair with the email)
  +  Profiles          YC founder page + a LinkedIn people-search link for a manual DM fallback

Confidence levels, best to worst:
  verified  a verifier confirmed the mailbox exists
  public    the founder's company address is published (site or GitHub)
  likely    Hunter found it, or the domain accepts all mail so it can't be confirmed
  inbox     a shared inbox the founders probably read
  guess     pattern guess, unverified
"""
from app.services.contacts.emails import (CONFIDENCE_RANK, INBOX_NAMES, email_matches_name, has_mx,  # noqa: F401
                                          hunter_find, pattern_guesses, split_name, verify)
from app.services.contacts.fetch import _get, _text, client, normalize_domain  # noqa: F401
from app.services.contacts.jobs import open_roles  # noqa: F401
from app.services.contacts.names import linkedin_search_url, names_from_site_text, yc_founders  # noqa: F401
from app.services.contacts.waterfall import best_contact, enrich  # noqa: F401
from app.services.contacts.website import crawl_site, github_emails  # noqa: F401
