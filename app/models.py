"""Database tables.

User ─ Profile (1:1)        what the user wants and what they bring
Startup                      shared pool of discovered companies
Match (user × startup)       why a startup fits a user, plus its status
Outreach (per match)         the email draft → sent → followed up → replied
ChatMessage                  web chat history
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat stored naive datetimes as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))  # "" for Google-only accounts
    google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # Google account id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # WhatsApp-first accounts: the number is the identity (email is a placeholder until Gmail connects)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True, index=True)
    whatsapp_last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when they click the confirmation link (or sign in with Google). Sending is locked until then.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Bumped to sign out every existing session (e.g. when Google takes over an unconfirmed account)
    session_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Last time they used the app (web or WhatsApp); daily matching skips people who went quiet
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # AI drafts used today (cost cap); day is an ISO date string
    drafts_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    drafts_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Free trial (starts at the first paid action) and paid passes (see app/services/billing.py)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_drafts_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pass_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile: Mapped["Profile"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    resume_text: Mapped[str] = mapped_column(Text, default="")
    linkedin_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # their LinkedIn "Save to PDF" export
    summary: Mapped[str] = mapped_column(Text, default="")  # LLM-written one-paragraph background
    skills: Mapped[list] = mapped_column(JSON, default=list)
    highlights: Mapped[list] = mapped_column(JSON, default=list)  # concrete wins with numbers
    target_roles: Mapped[list] = mapped_column(JSON, default=list)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)  # stages, remote, locations, sectors
    links: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="profile")

    def is_ready(self) -> bool:
        return bool(self.summary and (self.target_roles or self.skills))


class Startup(Base):
    __tablename__ = "startups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    one_liner: Mapped[str] = mapped_column(Text, default="")
    stage: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    signal: Mapped[str] = mapped_column(Text, default="")  # why now: "raised $4M seed", "hiring 3 engineers"
    source: Mapped[str] = mapped_column(String(64), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    founders: Mapped[list] = mapped_column(JSON, default=list)  # [{name, title, source, linkedin_search, profile_url}]
    contacts: Mapped[list | None] = mapped_column(JSON, nullable=True)  # ranked [{email, name, confidence, source}]
    jobs: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{title, location, remote, url, board}]
    github_org: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("user_id", "startup_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    startup_id: Mapped[int] = mapped_column(ForeignKey("startups.id"))
    score: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    # new → drafted → sent → replied   |  skipped
    status: Mapped[str] = mapped_column(String(32), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    user: Mapped[User] = relationship()
    startup: Mapped[Startup] = relationship()
    outreach: Mapped["Outreach"] = relationship(back_populates="match", uselist=False, cascade="all, delete-orphan")


class Outreach(Base):
    __tablename__ = "outreach"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    to_email: Mapped[str] = mapped_column(String(255), default="")
    to_name: Mapped[str] = mapped_column(String(255), default="")
    to_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)  # verified/public/likely/inbox/guess/user
    dm_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # LinkedIn/X note for a manual DM
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # draft → sent (the user sent it themselves) → replied | cancelled
    status: Mapped[str] = mapped_column(String(32), default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    followup_draft: Mapped[str | None] = mapped_column(Text, nullable=True)  # due follow-up, waiting for the user to send
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    followups_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reply_snippet: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    match: Mapped[Match] = relationship(back_populates="outreach")


class LLMUsage(Base):
    """One row per model call: the measurement channel for what AI actually costs (see app/cost_report.py)."""
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class WhatsAppInbound(Base):
    """Message ids already handled: Meta retries webhooks, and a retry must not run the agent twice."""
    __tablename__ = "whatsapp_inbound"
    id: Mapped[int] = mapped_column(primary_key=True)
    wa_message_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    unread: Mapped[bool] = mapped_column(Boolean, default=False)  # agent-initiated notifications
    # Startup cards shown with this reply, as match ids only ([{"type": "matches", "ids": [...]},
    # {"type": "draft", "id": n}]); rebuilt from current data on load, so they show today's state.
    cards: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Payment(Base):
    """One PhonePe order. Kept after account deletion (user_id is then cleared) for refunds and accounts."""
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    merchant_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phonepe_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan: Mapped[str] = mapped_column(String(16))
    amount_paise: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="CREATED")  # CREATED, PENDING, COMPLETED, FAILED
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # pass turned on
