"""Policy pages: Terms, Privacy, Refunds, Contact. Payment gateways check for these.

Business details come from settings (BUSINESS_NAME, SUPPORT_EMAIL, BUSINESS_ADDRESS) so they
can be filled in without touching the text. The wording describes how FounderReach actually
works; review it (ideally with a lawyer) before relying on it.
"""
from html import escape

from app.config import get_settings

UPDATED = "26 October 2026"

_SHELL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · FounderReach</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <main class="legal">
    <a class="logo" href="/"><i></i>FounderReach</a>
    <h1>{title}</h1>
    <p class="fine">Last updated {updated}</p>
    {body}
    <nav class="legal-nav"><a href="/terms">Terms</a><a href="/privacy">Privacy</a><a href="/refunds">Refunds</a><a href="/contact">Contact</a><a href="/">Home</a></nav>
  </main>
</body>
</html>"""


def _who() -> dict:
    s = get_settings()
    email = s.support_email.strip()
    return {
        "name": escape(s.business_name),
        "email": f'<a href="mailto:{escape(email)}">{escape(email)}</a>' if email else 'us through our <a href="/contact">Contact page</a>',
        "address": escape(s.business_address.strip()),
    }


def _page(title: str, body: str) -> str:
    return _SHELL.format(title=escape(title), updated=UPDATED, body=body)


def terms() -> str:
    w = _who()
    return _page("Terms & Conditions", f"""
<p>FounderReach ("we", "us") is run by {w['name']}. By creating an account or using FounderReach you agree to these terms.</p>

<h2>What FounderReach does</h2>
<p>FounderReach finds early-stage startups in Bangalore that may fit your background, shows publicly available details about
their founders, and writes draft emails and follow-ups for you. <strong>We never send email on your behalf</strong> and never
access your email inbox: you choose whether to send each draft, from your own email account.</p>

<h2>Your account</h2>
<ul>
  <li>You must give accurate information and keep your password safe. You're responsible for activity on your account.</li>
  <li>You must be at least 18, or have a parent's or guardian's permission.</li>
  <li>Some features (founder emails and drafts) unlock only after you confirm your email address or sign in with Google.</li>
</ul>

<h2>Using it fairly</h2>
<ul>
  <li>Use founder contact details only to reach out about jobs or work with that startup, one considered message at a time.</li>
  <li>Don't use FounderReach to spam, harass, mislead, or send bulk or automated email, and follow the anti-spam and privacy
      laws that apply to you.</li>
  <li>You're responsible for the emails and messages you send, including checking that drafts are accurate before sending.</li>
  <li>Don't copy, resell or scrape the startup or founder data, or try to break or overload the service.</li>
</ul>

<h2>AI-written content and data</h2>
<p>Drafts, summaries and match reasons are written by AI and can contain mistakes. Founder emails are found from public sources and
are labelled with how confident we are; some may be wrong or bounce. We don't promise that anyone will reply or that you'll get an
interview or job.</p>

<h2>Free trial and passes</h2>
<ul>
  <li>New accounts get a 2-day free trial. After that, using FounderReach for new matches, drafts and follow-up reminders needs a pass:
      ₹100 for 7 days or ₹300 for 30 days (prices include applicable taxes unless stated otherwise at checkout).</li>
  <li>Passes don't renew automatically. When a pass ends you keep access to what you already have.</li>
  <li>Payments are handled by our payment partner; we never see or store your card details. Refunds follow our
      <a href="/refunds">Refund Policy</a>.</li>
</ul>

<h2>Ending your account</h2>
<p>You can delete your account at any time from the Pipeline tab. We may suspend accounts that break these terms.</p>

<h2>Liability</h2>
<p>FounderReach is provided "as is". To the extent the law allows, we're not liable for indirect or consequential losses, and our total
liability to you is limited to the amount you paid us in the 3 months before the claim.</p>

<h2>Changes and law</h2>
<p>We may update these terms and will show the date of the latest change above. These terms are governed by the laws of India, and the
courts in Bengaluru, Karnataka have jurisdiction. For questions, write to {w['email']}.</p>
""")


def privacy() -> str:
    w = _who()
    return _page("Privacy Policy", f"""
<p>This policy explains what FounderReach, run by {w['name']}, collects and why.</p>

<h2>What we collect from you</h2>
<ul>
  <li><strong>Account:</strong> your name and email, and a securely hashed password, or, if you sign in with Google, the name and email
      Google shares. We never get access to your Gmail or other Google data.</li>
  <li><strong>Your background:</strong> the resume or LinkedIn PDF you upload, your LinkedIn profile link, and the preferences you tell us.</li>
  <li><strong>Your activity:</strong> chat messages, matches, drafts, and which emails you mark as sent or replied.</li>
  <li><strong>WhatsApp (if you use it):</strong> your phone number and the messages you send to our WhatsApp number.</li>
  <li><strong>Payments:</strong> which pass you bought and when. Card and UPI details are handled by our payment partner, not us.</li>
</ul>
<p>Your browser stores a sign-in token so you stay signed in. We don't use advertising or tracking cookies.</p>

<h2>How we use it</h2>
<p>To match you with startups, write drafts and reminders in your voice, run your account, prevent abuse, and keep the service working.
We don't sell your data.</p>

<h2>Who processes it for us</h2>
<ul>
  <li><strong>Anthropic</strong> (Claude AI): reads your background and messages to write matches, drafts and replies.</li>
  <li><strong>Our hosting provider</strong>, where the app and its database run.</li>
  <li><strong>Google</strong> (if you sign in with Google), <strong>Meta</strong> (if you use WhatsApp), and our email provider for
      account emails such as confirmation links.</li>
  <li><strong>Our payment partner</strong>, when you buy a pass.</li>
</ul>

<h2>Founders and startups</h2>
<p>Startup and founder details come from public sources: startup news, public company websites and public code repositories, Y Combinator,
and <a href="https://www.bangalorestartupmap.com/" target="_blank" rel="noopener">Bangalore Startup Map</a>. We never log in to or scrape
LinkedIn. If you're a founder and want your details removed, write to {w['email']}.</p>

<h2>Keeping and deleting your data</h2>
<p>We keep your data while your account exists. Delete your account from the Pipeline tab, or write to {w['email']}, and we remove your
profile, uploaded documents, chats, matches and drafts.</p>

<h2>Security</h2>
<p>Passwords are hashed, connections are encrypted, and access to the database is restricted. No system is perfectly secure, so please
use a strong, unique password.</p>

<h2>Contact</h2>
<p>For privacy questions or requests, write to {w['email']}. We'll show the date of any change to this policy above.</p>
""")


def refunds() -> str:
    w = _who()
    return _page("Refund & Cancellation Policy", f"""
<p>This policy covers payments to FounderReach, run by {w['name']}.</p>
<ul>
  <li><strong>Free trial:</strong> every new account gets 2 days free. No payment details are needed for the trial.</li>
  <li><strong>Passes:</strong> ₹100 for 7 days or ₹300 for 30 days. Passes are one-time payments and <strong>never renew
      automatically</strong>, so there's nothing to cancel.</li>
  <li><strong>Refunds:</strong> if you haven't used a pass yet (no new matches or drafts since buying it), you can ask for a full refund
      within 24 hours of buying it. After that, or once it's been used, passes aren't refundable.</li>
  <li><strong>Charged by mistake?</strong> If you were charged twice or a payment failed but money left your account, contact us and we'll
      refund the extra amount.</li>
  <li><strong>How:</strong> write to {w['email']} with the email address on your account and the payment reference. Approved refunds go back to
      the original payment method, usually within 5–7 working days depending on your bank.</li>
</ul>
""")


def contact() -> str:
    w = _who()
    address = f"<p><strong>Address:</strong><br>{w['address']}</p>" if w["address"] else ""
    email = w["email"] if get_settings().support_email.strip() else "coming soon"
    return _page("Contact Us", f"""
<p>FounderReach is run by <strong>{w['name']}</strong>, in Bengaluru, Karnataka, India.</p>
<p><strong>Email:</strong> {email}</p>
{address}
<p>We usually reply within 2 working days. For refunds, include the email address on your account and your payment reference.</p>
""")
