"""ORM models for the core tables described in docs/design/system-design.md §4.

Kept deliberately close to the design doc. JSON columns use JSONB on Postgres and JSON elsewhere
so that model-level tests can run on SQLite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bidtriage.core.ids import new_id

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType}


def _pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="estimator")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    digest_prefs: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # graph | imap | file | manual
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mailbox: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    config_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delta_state: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # SPEC-01 F7: first-connection backfill, oldest-first, rate-limited behind live polls.
    backfill_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    backfill_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backfill_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backfill_last_error: Mapped[str | None] = mapped_column(Text)
    backfill_stuck: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # SPEC-01 F8: the alert for a `down` source fires once per outage, not once per poll.
    down_alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourcePoll(Base):
    __tablename__ = "source_polls"
    id: Mapped[str] = _pk()
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="live")  # live | backfill
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    __table_args__ = (Index("ix_source_polls_source_started", "source_id", "started_at"),)


class SourceSkip(Base):
    """A message ingestion could not fetch or store, kept so the loss is visible (SPEC-10 F1).

    Rows appear only after `INGEST_MAX_FETCH_ATTEMPTS` consecutive failures, at which point the
    source cursor is allowed past the message and it is surfaced for review.
    """

    __tablename__ = "source_skips"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    first_seen_at: Mapped[datetime] = _ts()
    last_attempt_at: Mapped[datetime] = _ts()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error: Mapped[str | None] = mapped_column(Text)
    given_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawMessage(Base):
    __tablename__ = "raw_messages"
    id: Mapped[str] = _pk()
    internet_message_id: Mapped[str | None] = mapped_column(String(998), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    from_addr: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    from_name: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    to: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    cc: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at_confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="high")
    received_at: Mapped[datetime] = _ts()
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_trimmed: Mapped[str] = mapped_column(Text, nullable=False, default="")
    headers: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    forwarded_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    forwarded_by_addr: Mapped[str | None] = mapped_column(String(320))
    forward_note: Mapped[str | None] = mapped_column(Text)
    forward_chain: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attachments_truncated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str | None] = mapped_column(String(30), index=True)
    kind_confidence: Mapped[float | None] = mapped_column(Float)
    extraction_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = _ts()

    attachments: Mapped[list[RawAttachment]] = relationship(back_populates="message")

    __table_args__ = (
        Index("ix_raw_messages_hash_received", "content_hash", "received_at"),
        # Partial so the many messages with no Message-ID header do not collide with each other.
        Index(
            "uq_raw_messages_internet_message_id",
            "internet_message_id",
            unique=True,
            postgresql_where=text("internet_message_id IS NOT NULL"),
            sqlite_where=text("internet_message_id IS NOT NULL"),
        ),
    )


class MessageSource(Base):
    __tablename__ = "message_sources"
    message_id: Mapped[str] = mapped_column(ForeignKey("raw_messages.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    recipient_path: Mapped[str] = mapped_column(String(320), nullable=False, default="")


class RawAttachment(Base):
    __tablename__ = "raw_attachments"
    id: Mapped[str] = _pk()
    message_id: Mapped[str] = mapped_column(
        ForeignKey("raw_messages.id"), nullable=False, index=True
    )
    # Set for members extracted out of a container (ZIP); the container keeps its own row.
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("raw_attachments.id"), index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/octet-stream"
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    blob_key: Mapped[str | None] = mapped_column(String(256))
    text: Mapped[str | None] = mapped_column(Text)
    pages: Mapped[int | None] = mapped_column(Integer)
    ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    large_document: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    oversize: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extraction_error: Mapped[str | None] = mapped_column(String(64))

    message: Mapped[RawMessage] = relationship(back_populates="attachments")


class MessageLink(Base):
    __tablename__ = "message_links"
    id: Mapped[str] = _pk()
    message_id: Mapped[str] = mapped_column(
        ForeignKey("raw_messages.id"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    host_class: Mapped[str] = mapped_column(String(40), nullable=False, default="other")
    wrapped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    label: Mapped[str | None] = mapped_column(String(256))


class Extraction(Base):
    __tablename__ = "extractions"
    id: Mapped[str] = _pk()
    message_id: Mapped[str] = mapped_column(
        ForeignKey("raw_messages.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    superseded_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = _ts()


class GC(Base):
    __tablename__ = "gcs"
    id: Mapped[str] = _pk()
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="gc")
    tier: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    tier_reason: Mapped[str | None] = mapped_column(Text)
    tier_set_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    tier_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    key_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payment_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_from: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    created_at: Mapped[datetime] = _ts()

    aliases: Mapped[list[GCAlias]] = relationship(back_populates="gc", cascade="all, delete-orphan")
    domains: Mapped[list[GCDomain]] = relationship(
        back_populates="gc", cascade="all, delete-orphan"
    )


class GCAlias(Base):
    __tablename__ = "gc_aliases"
    gc_id: Mapped[str] = mapped_column(ForeignKey("gcs.id"), primary_key=True)
    alias: Mapped[str] = mapped_column(String(200), primary_key=True)
    gc: Mapped[GC] = relationship(back_populates="aliases")


class GCDomain(Base):
    __tablename__ = "gc_domains"
    gc_id: Mapped[str] = mapped_column(ForeignKey("gcs.id"), primary_key=True)
    domain: Mapped[str] = mapped_column(String(253), primary_key=True)
    gc: Mapped[GC] = relationship(back_populates="domains")


class GCContact(Base):
    __tablename__ = "gc_contacts"
    id: Mapped[str] = _pk()
    gc_id: Mapped[str] = mapped_column(ForeignKey("gcs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    role: Mapped[str | None] = mapped_column(String(100))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GCStats(Base):
    __tablename__ = "gc_stats"
    gc_id: Mapped[str] = mapped_column(ForeignKey("gcs.id"), primary_key=True)
    window: Mapped[str] = mapped_column(String(10), primary_key=True, default="12m")
    invites: Mapped[int] = mapped_column(Integer, default=0)
    bids: Mapped[int] = mapped_column(Integer, default=0)
    submitted: Mapped[int] = mapped_column(Integer, default=0)
    won: Mapped[int] = mapped_column(Integer, default=0)
    hit_rate: Mapped[float | None] = mapped_column(Float)
    avg_days_notice: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = _ts()


class Opportunity(Base):
    __tablename__ = "opportunities"
    id: Mapped[str] = _pk()
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new", index=True)
    gc_id: Mapped[str | None] = mapped_column(ForeignKey("gcs.id"), index=True)
    canonical: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    normalized_name: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(40), nullable=False, default="", index=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    first_seen_at: Mapped[datetime] = _ts()
    last_activity_at: Mapped[datetime] = _ts()
    assignee_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_since_digest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    locked_fields: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    related_project_ids: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpportunitySource(Base):
    __tablename__ = "opportunity_sources"
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("raw_messages.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="origin")
    attached_at: Mapped[datetime] = _ts()
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class Addendum(Base):
    __tablename__ = "addenda"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    number: Mapped[int | None] = mapped_column(Integer)
    message_id: Mapped[str] = mapped_column(ForeignKey("raw_messages.id"), nullable=False)
    received_at: Mapped[datetime] = _ts()
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    __table_args__ = (UniqueConstraint("opportunity_id", "label", name="uq_addenda_opp_label"),)


class FieldHistory(Base):
    __tablename__ = "field_history"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id"), nullable=False, index=True
    )
    field: Mapped[str] = mapped_column(String(60), nullable=False)
    old: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    new: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("raw_messages.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    changed_at: Mapped[datetime] = _ts()


class ScoringProfile(Base):
    __tablename__ = "scoring_profiles"
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _ts()


class Score(Base):
    __tablename__ = "scores"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id"), nullable=False, index=True
    )
    profile_version: Mapped[int] = mapped_column(
        ForeignKey("scoring_profiles.version"), nullable=False
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    band: Mapped[str] = mapped_column(String(20), nullable=False)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = _ts()


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="review")
    reason: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    undone_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = _ts()


class Outcome(Base):
    __tablename__ = "outcomes"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id"), nullable=False, index=True
    )
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_price: Mapped[float | None] = mapped_column(Float)
    award_price: Mapped[float | None] = mapped_column(Float)
    competitor: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class ActionTokenUsed(Base):
    __tablename__ = "action_tokens_used"
    opportunity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action: Mapped[str] = mapped_column(String(30), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    used_at: Mapped[datetime] = _ts()


class Digest(Base):
    __tablename__ = "digests"
    id: Mapped[str] = _pk()
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    recipient_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    html: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="built")


class DigestWatermark(Base):
    __tablename__ = "digest_watermarks"
    recipient_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    last_scheduled_digest_at: Mapped[datetime] = _ts()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    run_at: Mapped[datetime] = _ts()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_jobs_status_run_at_priority", "status", "run_at", "priority"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = _pk()
    actor_user_id: Mapped[str | None] = mapped_column(String(36))
    role: Mapped[str | None] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="system")
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id", "created_at"),)


class CalendarToken(Base):
    __tablename__ = "calendar_tokens"
    id: Mapped[str] = _pk()
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="personal")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
