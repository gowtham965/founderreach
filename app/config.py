"""App settings, loaded from environment variables or a .env file."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "FounderReach"
    # Shown on the Terms, Privacy, Refunds and Contact pages (payment gateways check these)
    business_name: str = "Gowtham R Gowda"
    support_email: str = ""
    business_address: str = ""  # full postal address; payment gateways usually require it
    # Emails allowed to run admin actions (e.g. refreshing the startup pool), comma separated
    admin_emails: str = ""
    base_url: str = "http://localhost:8000"
    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite:///./founderreach.db"

    # LLM: "anthropic" | "ollama" | "fake" (tests / offline demo)
    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"  # chat, drafts, follow-ups: the user reads these
    # High-volume, checkable work (news extraction, startup scoring, founder/profile extraction).
    # Set to the same as ANTHROPIC_MODEL to undo. Not yet validated by an eval on this app.
    anthropic_model_fast: str = "claude-haiku-5-5"
    # Email drafts, rewrites, follow-ups and LinkedIn notes: ~20x cheaper than Sonnet 5.5 per token.
    # Set to claude-sonnet-5-5 if drafts read worse (compare 20-30 real drafts once the key is in).
    anthropic_model_draft: str = "claude-haiku-5-5"
    # Thinking effort per kind of work (low | medium | high | xhigh | max; empty = model default).
    # Not yet validated by an eval on this app; empty all three to restore the defaults.
    effort_chat: str = "medium"
    effort_extract: str = "low"
    effort_draft: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b"

    # Founder email lookup (optional; falls back to pattern guessing)
    hunter_api_key: str = ""
    # Optional: raises GitHub API limit from 60 to 5,000 requests/hour (no scopes needed)
    github_token: str = ""

    # App email (sign-up confirmation links), sent over SMTP. Gmail: turn on 2-step verification,
    # create an app password at myaccount.google.com/apppasswords, put it in SMTP_PASSWORD.
    # Empty = nothing is sent; the link is printed in the server log instead.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from_name: str = "FounderReach"

    # WhatsApp (Meta WhatsApp Cloud API). From developers.facebook.com → your app → WhatsApp → API Setup.
    # The bot never starts conversations: it only replies to a message the person just sent.
    whatsapp_token: str = ""            # access token (temporary for testing, or a System User token)
    whatsapp_phone_number_id: str = ""  # the bot number's ID (not the number itself)
    whatsapp_verify_token: str = ""     # any secret string; paste the same into Meta's webhook setup
    whatsapp_app_secret: str = ""       # App settings → Basic → App secret; verifies webhook signatures
    whatsapp_number: str = ""           # the bot's number in international format, e.g. 15551234567 (for wa.me links)
    whatsapp_api_version: str = "v21.0"

    # Sign in with Google (Google Cloud "Web application" OAuth client). Name and email only:
    # FounderReach never asks for Gmail access, so no Google security review is needed.
    google_client_id: str = ""
    google_client_secret: str = ""

    # Discovery sources (comma separated RSS feeds). Indian startup news, for the Bangalore focus.
    news_feeds: str = (
        "https://inc42.com/buzz/feed/,"
        "https://entrackr.com/rss,"
        "https://yourstory.com/feed,"
        "https://economictimes.indiatimes.com/tech/startups/rssfeeds/78570540.cms"
    )
    yc_hiring_url: str = "https://yc-oss.github.io/api/companies/hiring.json"

    # Which startups make it into the pool and into matches. Comma separated; empty = no limit.
    # To expand later: add cities ("bengaluru,bangalore,mumbai") or stages ("pre-seed,seed,series a").
    focus_cities: str = "bengaluru,bangalore"
    focus_stages: str = "pre-seed,seed"
    # YC marks most young companies "Early"; only count recent batches as seed stage.
    yc_seed_batch_years: int = 2

    # Bangalore Startup Map (bangalorestartupmap.com), an independent directory. One page read a
    # week; only facts kept. Used with the owner's permission on condition of credit with a
    # link to her site: keep the "via Bangalore Startup Map" links (README → Bangalore focus).
    startup_map_enabled: bool = True
    startup_map_url: str = "https://www.bangalorestartupmap.com/"
    startup_map_refresh_days: int = 7
    startup_map_max_age_years: int = 5  # seed companies founded longer ago are unlikely to have just raised

    # Humanize pass on drafts/follow-ups: always | when_tells | off
    humanize_mode: str = "always"

    # Guardrails
    max_drafts_per_day: int = 15  # AI drafts per user per day (new, rewrites, follow-ups): bounds AI cost
    followup_days: str = "3,7"  # follow-up reminders, days after the user marks the email sent
    daily_matches: int = 5
    scheduler_enabled: bool = True
    digest_hour_ist: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
