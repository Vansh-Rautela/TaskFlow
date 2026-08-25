"""SQLAlchemy ORM table definitions.

Every table has a `tenant_id` column.
Only two UNIQUE constraints are safety-critical and tested:
  - inbox.dedupe_key  (first line of deduplication)
  - outbox.idempotency_key  (send-once guarantee)

All other columns follow the schemas in docs/04_SCHEMAS.md.
Do not add columns without updating 04_SCHEMAS.md in the same commit.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------- ingestion ----------


class InboxRow(Base):
    __tablename__ = "inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    thread_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provider_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_thread_headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)


# ---------- tracing ----------


class TraceRow(Base):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_channel: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    intent_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    classifier_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Complex nested objects serialised as JSON blobs
    retrieval_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validators_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decision_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    llm_calls_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    delivery_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    errors_json: Mapped[list | None] = mapped_column(JSON, nullable=True)


class TraceEventRow(Base):
    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ---------- human loop ----------


class ReviewRow(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    draft_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decision_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sla_deadline: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sender_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    inbound_body: Mapped[str | None] = mapped_column(String, nullable=True)


class EditRecordRow(Base):
    __tablename__ = "edit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_draft: Mapped[str] = mapped_column(Text, nullable=False)
    edited_draft: Mapped[str] = mapped_column(Text, nullable=False)
    editor: Mapped[str] = mapped_column(String(256), nullable=False)
    edited_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_diff: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------- delivery ----------


class OutboxRow(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outbound_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_outbox_idempotency_key"),)


# ---------- observability ----------


class LLMCallRow(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    repaired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_over: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    called_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AuditRow(Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AlertRow(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
