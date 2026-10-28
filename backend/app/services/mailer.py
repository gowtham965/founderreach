"""App email (not the user's Gmail): sign-up confirmation links, over SMTP.

Set SMTP_USER / SMTP_PASSWORD (a Gmail app password works). Without them nothing is
sent and the link goes to the server log, so local and demo runs still work.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape

import certifi

from app.config import get_settings

log = logging.getLogger(__name__)


class MailError(Exception):
    pass


def is_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_user and s.smtp_password)


def send(to: str, subject: str, text: str, html: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = f"{s.mail_from_name} <{s.smtp_user}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    # certifi's CA bundle: python.org macOS installs have no system certs for ssl to use
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(str(exc)) from exc


def send_confirmation(to: str, name: str, link: str) -> None:
    """Runs as a background task after sign-up / resend; failures are logged, never raised."""
    if not is_configured():
        log.warning("SMTP not configured. Confirmation link for %s: %s", to, link)
        return
    first = (name or "").split()[0] if (name or "").strip() else "there"
    text = (f"Hi {first},\n\nConfirm your email to start reaching founders with FounderReach:\n\n{link}\n\n"
            "The link works for 24 hours. If you didn't sign up, you can ignore this email.\n")
    html = f"""<div style="font-family:system-ui,sans-serif;max-width:480px;margin:auto;padding:24px;color:#1a1f2b">
  <p>Hi {escape(first)},</p>
  <p>Confirm your email to start reaching founders with FounderReach.</p>
  <p style="margin:28px 0"><a href="{escape(link)}" style="background:#6d8bff;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600">Confirm my email</a></p>
  <p style="color:#5b6478;font-size:14px">The link works for 24 hours. If you didn't sign up, you can ignore this email.</p>
</div>"""
    try:
        send(to, "Confirm your email for FounderReach", text, html)
        log.info("confirmation email sent to %s", to)
    except MailError as exc:
        log.error("confirmation email to %s failed: %s", to, exc)
