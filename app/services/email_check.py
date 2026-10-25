"""Free sign-up email checks: no paid API, just a typo list, a throwaway-inbox list and DNS.

check() returns a message to show the user, or None if the address looks deliverable.
DNS trouble on our side (timeouts, no resolver) lets the address through: a slow
network should never lock people out of signing up.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Big providers: known good, skip the DNS lookup.
KNOWN_GOOD = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "yahoo.co.in", "ymail.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "aol.com", "zoho.com", "zohomail.in", "rediffmail.com", "gmx.com", "fastmail.com", "hey.com",
}

# Common slips of the finger on the big providers.
TYPOS = {
    "gmial.com": "gmail.com", "gamil.com": "gmail.com", "gmai.com": "gmail.com", "gmal.com": "gmail.com",
    "gmail.co": "gmail.com", "gmail.cm": "gmail.com", "gmail.con": "gmail.com", "gmail.om": "gmail.com",
    "gnail.com": "gmail.com", "gmaill.com": "gmail.com", "gmail.in": "gmail.com", "gmail.comm": "gmail.com",
    "hotmial.com": "hotmail.com", "hotmai.com": "hotmail.com", "hotmail.co": "hotmail.com", "hotmail.con": "hotmail.com",
    "outlok.com": "outlook.com", "outllok.com": "outlook.com", "outlook.co": "outlook.com", "outlook.con": "outlook.com",
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yahoo.co": "yahoo.com", "yahoo.con": "yahoo.com",
    "icloud.co": "icloud.com", "iclod.com": "icloud.com", "icloud.con": "icloud.com",
}

# Throwaway inboxes. Not exhaustive; catches the ones people actually reach for.
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com", "grr.la", "10minutemail.com",
    "10minutemail.net", "tempmail.com", "temp-mail.org", "temp-mail.io", "tempmail.net", "tempmailo.com",
    "throwawaymail.com", "yopmail.com", "yopmail.net", "trashmail.com", "trashmail.de", "getnada.com", "nada.email",
    "dispostable.com", "maildrop.cc", "mailnesia.com", "mintemail.com", "mohmal.com", "fakeinbox.com",
    "emailondeck.com", "spamgourmet.com", "mailcatch.com", "burnermail.io", "moakt.com", "tmail.ws",
    "tempinbox.com", "getairmail.com", "dropmail.me", "emailfake.com", "fakemail.net", "mytemp.email",
    "minutemail.com", "inboxkitten.com", "mail.tm", "tmpmail.org", "tmpmail.net", "1secmail.com",
}


def domain_accepts_mail(domain: str) -> bool:
    """True if the domain can receive mail (MX, or A/AAAA as the RFC 5321 fallback)."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return True
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        # "Null MX" (a single record pointing at ".") means the domain says it takes no mail.
        return not all(str(r.exchange) == "." for r in answers)
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        for rtype in ("A", "AAAA"):
            try:
                dns.resolver.resolve(domain, rtype, lifetime=4)
                return True
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            except dns.exception.DNSException:
                return True
        return False
    except dns.exception.DNSException as exc:  # timeout, no nameservers: our problem, not theirs
        log.warning("email domain check skipped for %s: %s", domain, exc)
        return True


def check(email: str) -> str | None:
    local, _, domain = email.strip().lower().rpartition("@")
    if not local or not domain:
        return "That doesn't look like an email address."
    if domain in TYPOS:
        return f"Did you mean {local}@{TYPOS[domain]}?"
    if domain in DISPOSABLE or any(domain.endswith("." + d) for d in DISPOSABLE):
        return "Please use a permanent email address, not a temporary inbox."
    if domain in KNOWN_GOOD:
        return None
    if not domain_accepts_mail(domain):
        return f"{domain} can't receive email. Check the address for typos."
    return None
