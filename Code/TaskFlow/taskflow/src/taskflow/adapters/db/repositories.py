"""Concrete repository implementations backed by SQLite + SQLAlchemy.

Every class satisfies exactly one Protocol from ports.repositories.
Services depend on the Protocol; these classes are wired at startup via DI.

Rules:
  - No business logic here. persistence mechanics only.
  - Raise taskflow.domain.errors types, not SQLAlchemy exceptions.
  - Optimistic locking in ReviewRepository.transition uses a single UPDATE
    with a WHERE clause on the expected state; rowcount == 1 means we won.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskflow.adapters.db.orm import (
    EditRecordRow,
    InboxRow,
    OutboxRow,
    ReviewRow,
    TraceEventRow,
    TraceRow,
)
from taskflow.domain.enums import Channel, OutboxState, ReviewState
from taskflow.domain.errors import DuplicateMessage
from taskflow.domain.models import (
    EditRecord,
    InboundMessage,
    LLMCall,
    OutboundMessage,
    ReviewItem,
    RoutingDecision,
    Trace,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------- Processed message (inbox deduplification) ----------


class SQLiteProcessedMessageRepository:
    """Satisfies ports.repositories.ProcessedMessageRepository."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def try_claim(self, dedupe_key: str) -> bool:
        """Insert a row for dedupe_key.  Returns False if it already existed."""
        async with self._factory() as session, session.begin():
            try:
                session.add(
                    InboxRow(
                        message_id="placeholder",  # will be overwritten by InboxRepository.save
                        dedupe_key=dedupe_key,
                        tenant_id="",
                        channel="",
                        sender="",
                        body_text="",
                        body_redacted="",
                        provider_message_id="",
                        provider_thread_headers={},
                        received_at=_utcnow(),
                    )
                )
                await session.flush()
                return True
            except IntegrityError:
                return False


class SQLiteInboxRepository:
    """Full inbox persistence. Satisfies no Protocol directly but used by the worker.

    NOTE: try_claim is the dedup gate — call it before save. If try_claim returns False,
    raise DuplicateMessage and skip processing.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def save(self, msg: InboundMessage) -> None:
        row = InboxRow(
            message_id=msg.message_id,
            dedupe_key=msg.dedupe_key,
            tenant_id=msg.tenant_id,
            channel=msg.channel.value,
            sender=msg.sender,
            subject=msg.subject,
            body_text=msg.body_text,
            body_redacted=msg.body_redacted,
            thread_ref=msg.thread_ref,
            provider_message_id=msg.provider_message_id,
            provider_thread_headers=msg.provider_thread_headers,
            received_at=msg.received_at,
            raw_ref=msg.raw_ref,
        )
        async with self._factory() as session, session.begin():
            try:
                session.add(row)
            except IntegrityError as err:
                raise DuplicateMessage(msg.dedupe_key) from err

    async def try_claim(self, dedupe_key: str) -> bool:
        """Try to reserve a dedupe_key. Returns False if already seen."""
        async with self._factory() as session, session.begin():
            existing = await session.execute(
                select(InboxRow.id).where(InboxRow.dedupe_key == dedupe_key).limit(1)
            )
            if existing.scalar() is not None:
                return False
            # Reserve the slot with minimal columns
            session.add(
                InboxRow(
                    message_id=f"pending:{uuid.uuid4().hex[:8]}",
                    dedupe_key=dedupe_key,
                    tenant_id="",
                    channel="console",
                    sender="",
                    body_text="",
                    body_redacted="",
                    provider_message_id="",
                    provider_thread_headers={},
                    received_at=_utcnow(),
                )
            )
            try:
                await session.flush()
                return True
            except IntegrityError:
                return False


# ---------- Trace repository ----------


class SQLiteTraceRepository:
    """Satisfies ports.repositories.TraceRepository."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def start(self, msg: InboundMessage) -> Trace:
        trace = Trace(
            trace_id=uuid.uuid4().hex,
            conversation_id=msg.thread_ref or uuid.uuid4().hex,
            message_id=msg.message_id,
            tenant_id=msg.tenant_id,
            source_channel=msg.channel,
            started_at=_utcnow(),
        )
        row = TraceRow(
            trace_id=trace.trace_id,
            conversation_id=trace.conversation_id,
            message_id=trace.message_id,
            tenant_id=trace.tenant_id,
            source_channel=trace.source_channel.value,
            started_at=trace.started_at,
        )
        async with self._factory() as session, session.begin():
            session.add(row)
        return trace

    async def event(self, trace_id: str, stage: str, payload: dict, elapsed_ms: int) -> None:
        # Fetch tenant_id from the parent trace row
        async with self._factory() as session:
            result = await session.execute(
                select(TraceRow.tenant_id).where(TraceRow.trace_id == trace_id).limit(1)
            )
            tenant_id = result.scalar() or ""

        async with self._factory() as session, session.begin():
            session.add(
                TraceEventRow(
                    trace_id=trace_id,
                    tenant_id=tenant_id,
                    stage=stage,
                    payload=payload,
                    elapsed_ms=elapsed_ms,
                    at=_utcnow(),
                )
            )

    async def update(self, trace: Trace) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                update(TraceRow)
                .where(TraceRow.trace_id == trace.trace_id)
                .values(
                    intent=trace.intent.value if trace.intent else None,
                    intent_confidence=trace.intent_confidence,
                    classifier_version=trace.classifier_version,
                    retrieval_json=trace.retrieval.model_dump(mode="json")
                    if trace.retrieval
                    else None,
                    draft_json=trace.draft.model_dump(mode="json") if trace.draft else None,
                    validators_json=[v.model_dump(mode="json") for v in trace.validators],
                    confidence_json=trace.confidence.model_dump(mode="json")
                    if trace.confidence
                    else None,
                    decision_json=trace.decision.model_dump(mode="json")
                    if trace.decision
                    else None,
                    llm_calls_json=[c.model_dump(mode="json") for c in trace.llm_calls],
                    delivery_result=trace.delivery_result,
                    errors_json=trace.errors or [],
                )
            )

    async def finish(self, trace_id: str, decision: RoutingDecision) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                update(TraceRow)
                .where(TraceRow.trace_id == trace_id)
                .values(
                    finished_at=_utcnow(),
                    decision_json=decision.model_dump(mode="json"),
                )
            )

    async def get(self, trace_id: str) -> Trace | None:
        async with self._factory() as session:
            result = await session.execute(
                select(TraceRow).where(TraceRow.trace_id == trace_id).limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_trace(row)

    async def recent(self, limit: int = 50) -> list[Trace]:
        async with self._factory() as session:
            result = await session.execute(
                select(TraceRow).order_by(TraceRow.started_at.desc()).limit(limit)
            )
            return [_row_to_trace(r) for r in result.scalars()]


def _row_to_trace(row: TraceRow) -> Trace:
    from taskflow.domain.enums import Channel, Intent
    from taskflow.domain.models import (
        ConfidenceBreakdown,
        DraftOutput,
        RetrievalResult,
        RoutingDecision,
        ValidatorResult,
    )

    return Trace(
        trace_id=row.trace_id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        tenant_id=row.tenant_id,
        source_channel=Channel(row.source_channel),
        started_at=row.started_at,
        finished_at=row.finished_at,
        intent=Intent(row.intent) if row.intent else None,
        intent_confidence=row.intent_confidence,
        classifier_version=row.classifier_version,
        retrieval=RetrievalResult(**row.retrieval_json) if row.retrieval_json else None,
        draft=DraftOutput(**row.draft_json) if row.draft_json else None,
        validators=[ValidatorResult(**v) for v in (row.validators_json or [])],
        confidence=ConfidenceBreakdown(**row.confidence_json) if row.confidence_json else None,
        decision=RoutingDecision(**row.decision_json) if row.decision_json else None,
        llm_calls=[LLMCall(**c) for c in (row.llm_calls_json or [])],
        delivery_result=row.delivery_result,
        errors=row.errors_json or [],
    )


# ---------- Review repository ----------


class SQLiteReviewRepository:
    """Satisfies ports.repositories.ReviewRepository.

    transition() uses optimistic locking: UPDATE ... WHERE state = :expected.
    rowcount == 1 means we won; 0 means someone else got there first.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def create(self, item: ReviewItem) -> None:

        row = ReviewRow(
            review_id=item.review_id,
            trace_id=item.trace_id,
            conversation_id=item.conversation_id,
            tenant_id=item.tenant_id,
            state=item.state.value,
            draft_json=item.draft.model_dump(mode="json") if item.draft else None,
            decision_json=item.decision.model_dump(mode="json"),
            created_at=item.created_at,
            sla_deadline=item.sla_deadline,
            escalated_at=item.escalated_at,
            sender_email=item.sender_email,
            inbound_body=item.inbound_body,
        )
        async with self._factory() as session, session.begin():
            session.add(row)

    async def get(self, review_id: str) -> ReviewItem | None:
        async with self._factory() as session:
            result = await session.execute(
                select(ReviewRow).where(ReviewRow.review_id == review_id).limit(1)
            )
            row = result.scalar_one_or_none()
            return _row_to_review(row) if row else None

    async def pending(self) -> list[ReviewItem]:
        async with self._factory() as session:
            result = await session.execute(
                select(ReviewRow)
                .where(ReviewRow.state == ReviewState.PENDING.value)
                .order_by(ReviewRow.sla_deadline)
            )
            return [_row_to_review(r) for r in result.scalars()]

    async def due(self, now: datetime) -> list[ReviewItem]:
        async with self._factory() as session:
            result = await session.execute(
                select(ReviewRow).where(
                    ReviewRow.state == ReviewState.PENDING.value,
                    ReviewRow.sla_deadline <= now,
                )
            )
            return [_row_to_review(r) for r in result.scalars()]

    async def transition(self, review_id: str, to: ReviewState, expected: ReviewState) -> bool:
        """Optimistic lock. Returns False if the expected state wasn't found."""
        async with self._factory() as session, session.begin():
            cursor = cast(
                CursorResult[Any],
                await session.execute(
                    update(ReviewRow)
                    .where(
                        ReviewRow.review_id == review_id,
                        ReviewRow.state == expected.value,
                    )
                    .values(state=to.value)
                    .execution_options(synchronize_session="fetch")
                ),
            )
            return bool(cursor.rowcount == 1)

    async def record_edit(self, edit: EditRecord) -> None:
        row = EditRecordRow(
            review_id=edit.review_id,
            tenant_id="",  # filled from review lookup if needed
            original_draft=edit.original_draft,
            edited_draft=edit.edited_draft,
            editor=edit.editor,
            edited_at=edit.edited_at,
            reason=edit.reason,
            char_diff=edit.char_diff,
        )
        async with self._factory() as session, session.begin():
            session.add(row)


def _row_to_review(row: ReviewRow) -> ReviewItem:
    from taskflow.domain.models import DraftOutput, RoutingDecision

    return ReviewItem(
        review_id=row.review_id,
        trace_id=row.trace_id,
        conversation_id=row.conversation_id,
        tenant_id=row.tenant_id,
        state=ReviewState(row.state),
        draft=DraftOutput(**row.draft_json) if row.draft_json else None,
        decision=RoutingDecision(**row.decision_json),
        created_at=row.created_at,
        sla_deadline=row.sla_deadline,
        escalated_at=row.escalated_at,
        sender_email=row.sender_email,
        inbound_body=row.inbound_body,
    )


# ---------- Outbox repository ----------


class SQLiteOutboxRepository:
    """Satisfies ports.repositories.OutboxRepository.

    enqueue() returns False when idempotency_key already exists — first-line send-once.
    claim() moves QUEUED → SENDING atomically.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def enqueue(self, msg: OutboundMessage) -> bool:
        """Returns False when idempotency_key already in outbox."""
        row = OutboxRow(
            outbound_id=msg.outbound_id,
            conversation_id=msg.conversation_id,
            tenant_id=msg.tenant_id,
            channel=msg.channel.value,
            recipient=msg.recipient,
            subject=msg.subject,
            body_text=msg.body_text,
            reply_headers=msg.reply_headers,
            state=OutboxState.QUEUED.value,
            idempotency_key=msg.idempotency_key,
            retry_count=msg.retry_count,
        )
        async with self._factory() as session, session.begin():
            try:
                session.add(row)
                await session.flush()
                return True
            except IntegrityError:
                return False

    async def claim(self, limit: int = 10) -> list[OutboundMessage]:
        """Atomically move QUEUED → SENDING and return claimed rows."""
        async with self._factory() as session, session.begin():
            subq = (
                select(OutboxRow.outbound_id)
                .where(OutboxRow.state == OutboxState.QUEUED.value)
                .limit(limit)
                .scalar_subquery()
            )
            stmt = (
                update(OutboxRow)
                .where(
                    OutboxRow.outbound_id.in_(subq),
                    OutboxRow.state == OutboxState.QUEUED.value,
                )
                .values(state=OutboxState.SENDING.value)
                .returning(OutboxRow)
            )
            res = await session.execute(stmt)
            rows = list(res.scalars())
            return [_row_to_outbound(r) for r in rows]

    async def mark(self, outbound_id: str, state: OutboxState, error: str | None = None) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.outbound_id == outbound_id)
                .values(state=state.value, last_error=error)
            )


def _row_to_outbound(row: OutboxRow) -> OutboundMessage:
    return OutboundMessage(
        outbound_id=row.outbound_id,
        conversation_id=row.conversation_id,
        tenant_id=row.tenant_id,
        channel=Channel(row.channel),
        recipient=row.recipient,
        subject=row.subject,
        body_text=row.body_text,
        reply_headers=row.reply_headers or {},
        state=OutboxState(row.state),
        idempotency_key=row.idempotency_key,
        retry_count=row.retry_count,
    )
