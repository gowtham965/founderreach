"""Free sign-up email checks: typos, throwaway inboxes, domains that can't take mail."""
import dns.exception
import dns.resolver
import pytest

from app.services import email_check


def test_typo_suggests_fix():
    assert email_check.check("asha@gmial.com") == "Did you mean asha@gmail.com?"


def test_disposable_rejected():
    assert "temporary inbox" in email_check.check("x@mailinator.com")
    assert "temporary inbox" in email_check.check("x@eu.mailinator.com")


def test_known_provider_skips_dns(monkeypatch):
    monkeypatch.setattr(email_check, "domain_accepts_mail", lambda d: pytest.fail("no DNS for gmail"))
    assert email_check.check("Asha@Gmail.com") is None


def test_dead_domain_rejected(monkeypatch):
    monkeypatch.setattr(email_check, "domain_accepts_mail", lambda d: False)
    assert "can't receive email" in email_check.check("a@nope-not-real.dev")


class _Mx:
    def __init__(self, host): self.exchange = host


def _resolver(table):
    def resolve(domain, rtype, lifetime=None):
        r = table.get((domain, rtype))
        if isinstance(r, type) and issubclass(r, Exception):
            raise r()
        if r is None:
            raise dns.resolver.NoAnswer()
        return r
    return resolve


@pytest.mark.real_email_dns
@pytest.mark.parametrize("table,ok", [
    ({("a.dev", "MX"): [_Mx("mx.a.dev.")]}, True),
    ({("a.dev", "MX"): [_Mx(".")]}, False),                      # null MX: domain takes no mail
    ({("a.dev", "MX"): dns.resolver.NXDOMAIN}, False),            # domain doesn't exist
    ({("a.dev", "A"): ["1.2.3.4"]}, True),                       # no MX, falls back to A record
    ({}, False),                                                  # no MX, no A, no AAAA
    ({("a.dev", "MX"): dns.exception.Timeout}, True),             # our DNS is down: let them in
])
def test_domain_accepts_mail(monkeypatch, table, ok):
    monkeypatch.setattr(dns.resolver, "resolve", _resolver(table))
    assert email_check.domain_accepts_mail("a.dev") is ok
