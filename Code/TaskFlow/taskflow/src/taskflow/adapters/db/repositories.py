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
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskflow.adapters.db.orm import (
    AuditRow,
    EditRecordRow,
    InboxRow,
    OutboxRow,
    ReviewRow,
    TraceEventRow,
    TraceRow,
)
from taskflow.domain.enums import Channel, Intent, OutboxState, ReviewState
from taskflow.domain.errors import DuplicateMessage
from taskflow.domain.models import (
    Chunk,
    ConfidenceBreakdown,
    DraftOutput,
    EditRecord,
    InboundMessage,
    LLMCall,
    OutboundMessage,
    RetrievalResult,
    ReviewItem,
    RoutingDecision,
    ScoredChunk,
    Trace,
    ValidatorResult,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------- Inbox / deduplication ----------


class SQLiteInboxRepository:
    """Inbox persistence and deduplication.

    Satisfies ports.repositories.ProcessedMessageRepository.

    `try_claim` is the deduplication gate and the only place a dedupe_key is
    reserved. It relies on the UNIQUE constraint on `inbox.dedupe_key` rather than
    SELECT-then-INSERT, so two workers racing on the same message cannot both win.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def try_claim(self, dedupe_key: str) -> bool:
        """Reserve `dedupe_key`. Returns False if it was already taken.

        The INSERT is committed inside its own transaction. On IntegrityError the
        session is rolled back explicitly: leaving a failed flush to be committed by
        the context manager raises on the way out and turns a duplicate — an ordinary
        signal — into an exception.
        """
        async with self._factory() as session:
            session.add(
                InboxRow(
                    message_id=f"pending:{uuid.uuid4().hex[:8]}",
                    dedupe_key=dedupe_key,
                    tenant_id="",
                    channel=Channel.CONSOLE.value,
                    sender="",
                    body_text="",
                    body_redacted="",
                    provider_message_id="",
                    provider_thread_headers={},
                    received_at=_utcnow(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def save(self, msg: InboundMessage) -> None:
        """Fill in the row reserved by `try_claim` with the full message.

        Raises DuplicateMessage if no reservation exists and the key is already used.
        """
        async with self._factory() as session:
            values = {
                "message_id": msg.message_id,
                "tenant_id": msg.tenant_id,
                "channel": msg.channel.value,
                "sender": msg.sender,
                "subject": msg.subject,
                "body_text": msg.body_text,
                "body_redacted": msg.body_redacted,
                "thread_ref": msg.thread_ref,
                "provider_message_id": msg.provider_message_id,
                "provider_thread_headers": msg.provider_thread_headers,
                "received_at": msg.received_at,
                "raw_ref": msg.raw_ref,
            }
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(InboxRow).where(InboxRow.dedupe_key == msg.dedupe_key).values(**values)
                ),
            )
            if result.rowcount == 0:
                session.add(InboxRow(dedupe_key=msg.dedupe_key, **values))
            try:
                await session.commit()
            except IntegrityError as err:
                await session.rollback()
                raise DuplicateMessage(msg.dedupe_key) from err

    async def get(self, dedupe_key: str) -> InboundMessage | None:
        async with self._factory() as session:
            result = await session.execute(
                select(InboxRow).where(InboxRow.dedupe_key == dedupe_key).limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None or not row.message_id or row.message_id.startswith("pending:"):
                return None
            return InboundMessage(
                message_id=row.message_id,
                dedupe_key=row.dedupe_key,
                tenant_id=row.tenant_id,
                channel=Channel(row.channel),
                sender=row.sender,
                subject=row.subject,
                body_text=row.body_text,
                body_redacted=row.body_redacted,
                thread_ref=row.thread_ref,
                provider_message_id=row.provider_message_id,
                provider_thread_headers=row.provider_thread_headers or {},
                received_at=row.received_at,
                raw_ref=row.raw_ref,
            )


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


def _known_fields_only(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the model no longer declares."""
    return {k: v for k, v in data.items() if k in model.model_fields}


def _retrieval_from_json(data: dict[str, Any]) -> RetrievalResult:
    """Rehydrate a stored retrieval payload against the *current* models.

    A trace row is a historical record: one written before a field was removed still
    carries that field, and the domain models forbid extras. Reconciling the stored
    shape with the current one belongs here — the alternatives are keeping dead fields
    on the models forever, or making them permissive for live data too.
    """
    chunks = []
    for raw in data.get("chunks", []):
        scored = _known_fields_only(ScoredChunk, raw)
        if isinstance(scored.get("chunk"), dict):
            scored["chunk"] = _known_fields_only(Chunk, scored["chunk"])
        chunks.append(scored)

    return RetrievalResult(**{**_known_fields_only(RetrievalResult, data), "chunks": chunks})


def _row_to_trace(row: TraceRow) -> Trace:
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
        retrieval=_retrieval_from_json(row.retrieval_json) if row.retrieval_json else None,
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

    async def all(self, limit: int = 500) -> list[ReviewItem]:
        async with self._factory() as session:
            result = await session.execute(
                select(ReviewRow).order_by(ReviewRow.created_at.desc()).limit(limit)
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
        async with self._factory() as session:
            result = await session.execute(
                select(ReviewRow.tenant_id).where(ReviewRow.review_id == edit.review_id).limit(1)
            )
            tenant_id = result.scalar() or ""

        row = EditRecordRow(
            review_id=edit.review_id,
            tenant_id=tenant_id,
            original_draft=edit.original_draft,
            edited_draft=edit.edited_draft,
            editor=edit.editor,
            edited_at=edit.edited_at,
            reason=edit.reason,
            char_diff=edit.char_diff,
        )
        async with self._factory() as session, session.begin():
            session.add(row)

    async def edits_for(self, review_id: str) -> list[EditRecord]:
        async with self._factory() as session:
            result = await session.execute(
                select(EditRecordRow)
                .where(EditRecordRow.review_id == review_id)
                .order_by(EditRecordRow.edited_at)
            )
            return [_row_to_edit_record(r) for r in result.scalars()]


def _row_to_edit_record(row: EditRecordRow) -> EditRecord:
    return EditRecord(
        review_id=row.review_id,
        original_draft=row.original_draft,
        edited_draft=row.edited_draft,
        editor=row.editor,
        edited_at=row.edited_at,
        reason=row.reason,
        char_diff=row.char_diff,
    )


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
    claim()   moves QUEUED -> SENDING atomically and stamps claimed_at for the lease.
    mark()    records the true outcome: FAILED requeues, SENT and DEAD are terminal.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def enqueue(self, msg: OutboundMessage) -> bool:
        """Returns False when idempotency_key is already in the outbox."""
        async with self._factory() as session:
            session.add(
                OutboxRow(
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
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def claim(self, limit: int = 10) -> list[OutboundMessage]:
        """Atomically move QUEUED -> SENDING and return the claimed rows."""
        async with self._factory() as session, session.begin():
            subq = (
                select(OutboxRow.outbound_id)
                .where(OutboxRow.state == OutboxState.QUEUED.value)
                .order_by(OutboxRow.id)
                .limit(limit)
                .scalar_subquery()
            )
            res = await session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.outbound_id.in_(subq),
                    OutboxRow.state == OutboxState.QUEUED.value,
                )
                .values(state=OutboxState.SENDING.value, claimed_at=_utcnow())
                .returning(OutboxRow)
            )
            return [_row_to_outbound(r) for r in res.scalars()]

    async def mark(
        self,
        outbound_id: str,
        state: OutboxState,
        error: str | None = None,
        increment_retry: bool = False,
    ) -> None:
        """Record a delivery outcome.

        FAILED means "try again": the row goes back to QUEUED with its retry counter
        incremented, so the next poll picks it up. SENT and DEAD are terminal.
        """
        values: dict[str, Any] = {"last_error": error}

        if state is OutboxState.FAILED:
            values["state"] = OutboxState.QUEUED.value
            values["claimed_at"] = None
        else:
            values["state"] = state.value
            if state is OutboxState.SENT:
                values["claimed_at"] = None

        if increment_retry:
            values["retry_count"] = OutboxRow.retry_count + 1

        async with self._factory() as session, session.begin():
            await session.execute(
                update(OutboxRow).where(OutboxRow.outbound_id == outbound_id).values(**values)
            )

    async def requeue_expired(self, lease_minutes: int) -> int:
        """Return SENDING rows older than the lease to QUEUED. Returns how many."""
        cutoff = _utcnow() - timedelta(minutes=lease_minutes)
        async with self._factory() as session, session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(OutboxRow)
                    .where(
                        OutboxRow.state == OutboxState.SENDING.value,
                        OutboxRow.claimed_at.is_not(None),
                        OutboxRow.claimed_at < cutoff,
                    )
                    .values(
                        state=OutboxState.QUEUED.value,
                        claimed_at=None,
                        last_error="reclaimed after worker lease expiry",
                    )
                ),
            )
            return int(result.rowcount or 0)

    async def get(self, outbound_id: str) -> OutboundMessage | None:
        async with self._factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.outbound_id == outbound_id).limit(1)
            )
            row = result.scalar_one_or_none()
            return _row_to_outbound(row) if row else None


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
        last_error=row.last_error,
    )


# ---------- Audit repository ----------


class SQLiteAuditRepository:
    """Satisfies ports.repositories.AuditRepository.

    Human review endpoints accept an `operator` and a `reason`. Before this existed
    both were discarded, so the API promised an accountability record it did not keep.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def record(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._factory() as session, session.begin():
            session.add(
                AuditRow(
                    tenant_id=tenant_id,
                    actor=actor,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=payload or {},
                    at=_utcnow(),
                )
            )

    async def for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        async with self._factory() as session:
            result = await session.execute(
                select(AuditRow).where(AuditRow.entity_id == entity_id).order_by(AuditRow.at)
            )
            return [
                {
                    "actor": r.actor,
                    "action": r.action,
                    "entity_type": r.entity_type,
                    "entity_id": r.entity_id,
                    "payload": r.payload,
                    "at": r.at.isoformat(),
                }
                for r in result.scalars()
            ]
