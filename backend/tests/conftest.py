import os

# Settings are read once, by whichever test module imports the app first, so shared test
# settings live here rather than in one module.
# The flow tests use made-up startups with no city; keep the Bangalore focus off unless a test sets it.
os.environ.setdefault("FOCUS_CITIES", "")
os.environ.setdefault("FOCUS_STAGES", "")
os.environ.setdefault("SMTP_USER", "")  # never send real email from tests, whatever .env says
os.environ.setdefault("SMTP_PASSWORD", "")
os.environ["NTFY_TOPIC"] = ""  # never send real phone alerts from tests, whatever .env says

import pytest


@pytest.fixture(autouse=True)
def offline_email_domains(monkeypatch, request):
    """Tests run without network: treat every sign-up domain as able to receive mail."""
    if "real_email_dns" in request.keywords:
        return
    from app.services import email_check
    monkeypatch.setattr(email_check, "domain_accepts_mail", lambda d: True)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_email_dns: use email_check's own DNS logic (with a fake resolver)")


@pytest.fixture(autouse=True)
def reset_signup_limit():
    """Every test signs up from the same test client: start each one with a clean per-IP count."""
    from app import main
    main._SIGNUPS.clear()
