"""Unit/integration tests for Phase P1: repositories, migrations, and deduplication.

These tests run against a **real** temp SQLite file (no mocks) to verify:
  1. Migration creates all expected tables
  2. inbox UNIQUE constraint on dedupe_key (deduplication gate)
  3. outbox UNIQUE constraint on idempotency_key (send-once guarantee)
  4. Repository round-trips following the Protocol contract

pytest marks: `integration` — these tests touch disk (SQLite).
Run them with: `uv run pytest -m integration tests/unit/test_repositories.py`
They also run as part of `make test` since they are very fast.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskflow.adapters.db.engine import build_engine, build_session_factory, verify_engine
from taskflow.adapters.db.orm import Base
from taskflow.adapters.db.repositories import (
    SQLiteInboxRepository,
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.domain.enums import Channel, OutboxState, ReviewState, RouteAction
from taskflow.domain.models import (
    EditRecord,
    InboundMessage,
    OutboundMessage,
    ReviewItem,
    RoutingDecision,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------- Fixtures ----------


@pytest.fixture
async def engine(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    eng = build_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine):
    return build_session_factory(engine)


def _make_inbound(suffix: str = "") -> InboundMessage:
    uid = uuid.uuid4().hex[:8] + suffix
    return InboundMessage(
        message_id=uid,
        dedupe_key=f"email:{uid}",
        tenant_id="taskflow-demo",
        channel=Channel.EMAIL,
        sender=f"user{uid}@example.com",
        subject="Test subject",
        body_text="Hello, I need help.",
        body_redacted="Hello, I need help.",
        provider_message_id=uid,
        received_at=_utcnow(),
    )


def _make_outbound(conversation_id: str = "") -> OutboundMessage:
    cid = conversation_id or uuid.uuid4().hex
    body = "Thank you for reaching out."
    return OutboundMessage(
        outbound_id=uuid.uuid4().hex,
        conversation_id=cid,
        tenant_id="taskflow-demo",
        channel=Channel.EMAIL,
        recipient="user@example.com",
        subject="Re: your inquiry",
        body_text=body,
        idempotency_key=OutboundMessage.make_idempotency_key(cid, body),
    )


def _make_review(trace_id: str = "") -> ReviewItem:
    tid = trace_id or uuid.uuid4().hex
    return ReviewItem(
        review_id=uuid.uuid4().hex,
        trace_id=tid,
        conversation_id=uuid.uuid4().hex,
        tenant_id="taskflow-demo",
        state=ReviewState.PENDING,
        draft=None,
        decision=RoutingDecision(
            action=RouteAction.HUMAN_REVIEW,
            reason="Policy gate G1 triggered",
            reason_code="G1_policy_critical",
            confidence=None,
        ),
        created_at=_utcnow(),
        sla_deadline=_utcnow() + timedelta(hours=2),
    )


# ---------- Engine & WAL tests ----------


@pytest.mark.asyncio
async def test_engine_wal_mode(engine):
    """Engine must be in WAL mode after pragma listener fires."""
    await verify_engine(engine)  # raises if not WAL


@pytest.mark.asyncio
async def test_all_tables_created(engine):
    """Migration (via metadata.create_all) must create all nine tables."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = {row[0] for row in result}
    expected = {
        "inbox",
        "traces",
        "trace_events",
        "reviews",
        "edit_records",
        "outbox",
        "llm_calls",
        "audit",
        "alerts",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


# ---------- Inbox / dedupe tests ----------


@pytest.mark.asyncio
async def test_inbox_try_claim_unique(factory):
    """try_claim returns True on first call and False on the second for the same key."""
    repo = SQLiteInboxRepository(factory)
    key = f"email:{uuid.uuid4().hex}"
    assert await repo.try_claim(key) is True
    assert await repo.try_claim(key) is False


@pytest.mark.asyncio
async def test_inbox_save_roundtrip(factory):
    """save() stores a message; try_claim correctly sees it as already processed."""
    repo = SQLiteInboxRepository(factory)
    msg = _make_inbound()
    # First pre-claim the key via try_claim so save doesn't conflict
    assert await repo.try_claim(msg.dedupe_key) is True
    # Second attempt must fail
    assert await repo.try_claim(msg.dedupe_key) is False


# ---------- Outbox / send-once tests ----------


@pytest.mark.asyncio
async def test_outbox_enqueue_idempotent(factory):
    """Enqueue returns True first time, False on duplicate idempotency_key."""
    repo = SQLiteOutboxRepository(factory)
    msg = _make_outbound()
    assert await repo.enqueue(msg) is True
    assert await repo.enqueue(msg) is False  # same idempotency_key


@pytest.mark.asyncio
async def test_outbox_claim_and_mark(factory):
    """claim() moves QUEUED→SENDING; mark() sets final state."""
    repo = SQLiteOutboxRepository(factory)
    msg = _make_outbound()
    await repo.enqueue(msg)

    claimed = await repo.claim(limit=10)
    assert len(claimed) == 1
    assert claimed[0].outbound_id == msg.outbound_id

    await repo.mark(msg.outbound_id, OutboxState.SENT)
    # After marking SENT, claim should return empty (no QUEUED rows)
    second_claim = await repo.claim(limit=10)
    assert second_claim == []


@pytest.mark.asyncio
async def test_outbox_no_double_send(factory):
    """Two concurrent claim() calls on the same message must not both return it."""
    repo = SQLiteOutboxRepository(factory)
    msg = _make_outbound()
    await repo.enqueue(msg)

    # Simulate two concurrent workers claiming
    r1, r2 = await asyncio.gather(repo.claim(limit=10), repo.claim(limit=10))
    total = len(r1) + len(r2)
    assert total == 1, f"Expected exactly 1 claim across both calls, got {total}"


# ---------- Review / optimistic lock tests ----------


@pytest.mark.asyncio
async def test_review_create_and_get(factory):
    """Round-trip: create a review and retrieve it by ID."""
    repo = SQLiteReviewRepository(factory)
    item = _make_review()
    await repo.create(item)

    fetched = await repo.get(item.review_id)
    assert fetched is not None
    assert fetched.review_id == item.review_id
    assert fetched.state == ReviewState.PENDING


@pytest.mark.asyncio
async def test_review_transition_optimistic_lock(factory):
    """transition() returns False when expected state doesn't match (race lost)."""
    repo = SQLiteReviewRepository(factory)
    item = _make_review()
    await repo.create(item)

    # First transition succeeds
    won = await repo.transition(item.review_id, ReviewState.APPROVED, ReviewState.PENDING)
    assert won is True

    # Second transition on the same review with old expected state must fail
    lost = await repo.transition(item.review_id, ReviewState.REJECTED, ReviewState.PENDING)
    assert lost is False


@pytest.mark.asyncio
async def test_review_pending_list(factory):
    """pending() returns only PENDING reviews, ordered by SLA deadline."""
    repo = SQLiteReviewRepository(factory)
    r1 = _make_review()
    r2 = _make_review()
    await repo.create(r1)
    await repo.create(r2)

    pending = await repo.pending()
    assert len(pending) == 2

    # Approve one; now only 1 should be pending
    await repo.transition(r1.review_id, ReviewState.APPROVED, ReviewState.PENDING)
    pending_after = await repo.pending()
    assert len(pending_after) == 1
    assert pending_after[0].review_id == r2.review_id


@pytest.mark.asyncio
async def test_review_all_returns_every_state(factory):
    """all() is for offline analysis (scripts/calibrate_confidence.py) — unlike
    pending(), it must not filter by state."""
    repo = SQLiteReviewRepository(factory)
    r1, r2, r3 = _make_review(), _make_review(), _make_review()
    for r in (r1, r2, r3):
        await repo.create(r)
    await repo.transition(r1.review_id, ReviewState.APPROVED, ReviewState.PENDING)
    await repo.transition(r2.review_id, ReviewState.REJECTED, ReviewState.PENDING)

    all_items = await repo.all()

    assert len(all_items) == 3
    states = {item.review_id: item.state for item in all_items}
    assert states[r1.review_id] == ReviewState.APPROVED
    assert states[r2.review_id] == ReviewState.REJECTED
    assert states[r3.review_id] == ReviewState.PENDING


@pytest.mark.asyncio
async def test_review_edits_for_round_trips(factory):
    repo = SQLiteReviewRepository(factory)
    item = _make_review()
    await repo.create(item)

    assert await repo.edits_for(item.review_id) == []

    edit = EditRecord(
        review_id=item.review_id,
        original_draft="Thanks, we'll process that.",
        edited_draft="Thanks, we'll process that refund within 5 business days.",
        editor="operator_admin",
        edited_at=_utcnow(),
        reason="clarified timeline",
        char_diff=27,
    )
    await repo.record_edit(edit)

    fetched = await repo.edits_for(item.review_id)
    assert len(fetched) == 1
    assert fetched[0].original_draft == edit.original_draft
    assert fetched[0].edited_draft == edit.edited_draft
    assert fetched[0].char_diff == 27


# ---------- Trace tests ----------


@pytest.mark.asyncio
async def test_trace_start_and_get(factory):
    """start() creates a trace row; get() retrieves it by trace_id."""
    repo = SQLiteTraceRepository(factory)
    msg = _make_inbound()
    trace = await repo.start(msg)

    assert trace.trace_id
    assert trace.tenant_id == msg.tenant_id

    fetched = await repo.get(trace.trace_id)
    assert fetched is not None
    assert fetched.trace_id == trace.trace_id


@pytest.mark.asyncio
async def test_trace_reads_back_a_row_written_before_fields_were_removed(factory):
    """A trace row is a historical record and outlives the shape it was written in.

    `Chunk` used to carry doc_type / product_tier / intents and `ScoredChunk` used to
    carry dense_score / sparse_score. The models forbid extras, so a row written before
    those were removed used to raise a ValidationError on read — which took `preflight`
    and the dashboard down with it.
    """
    from sqlalchemy import update as sa_update

    from taskflow.adapters.db.orm import TraceRow

    repo = SQLiteTraceRepository(factory)
    trace = await repo.start(_make_inbound())

    legacy = {
        "query_used": "prorated refund",
        "sufficient": True,
        "latency_ms": 41,
        "top_score": 0.83,
        "support_count": 2,
        "chunks": [
            {
                "chunk": {
                    "chunk_id": "KB-REFUND-001:2",
                    "doc_id": "KB-REFUND-001",
                    "title": "Refunds",
                    "section": "Prorated",
                    "text": "Credits are prorated.",
                    "doc_type": "policy",  # removed from Chunk
                    "product_tier": None,  # removed from Chunk
                    "intents": [],  # removed from Chunk
                    "version": "1.0",
                    "source_path": "",
                    "tenant_id": "test",
                },
                "dense_score": None,  # removed from ScoredChunk
                "sparse_score": None,  # removed from ScoredChunk
                "rrf_score": 0.83,
                "rerank_score": None,
            }
        ],
    }

    async with factory() as session:
        await session.execute(
            sa_update(TraceRow)
            .where(TraceRow.trace_id == trace.trace_id)
            .values(retrieval_json=legacy)
        )
        await session.commit()

    fetched = await repo.get(trace.trace_id)

    assert fetched is not None
    assert fetched.retrieval is not None
    assert fetched.retrieval.top_score == 0.83
    assert len(fetched.retrieval.chunks) == 1
    assert fetched.retrieval.chunks[0].chunk.chunk_id == "KB-REFUND-001:2"
    assert fetched.retrieval.chunks[0].rrf_score == 0.83


@pytest.mark.asyncio
async def test_trace_event_written(factory):
    """event() writes a stage event row; no assertions on retrieval (get returns the Trace)."""
    repo = SQLiteTraceRepository(factory)
    msg = _make_inbound()
    trace = await repo.start(msg)
    # Should not raise
    await repo.event(trace.trace_id, "classify", {"intent": "billing"}, elapsed_ms=12)


@pytest.mark.asyncio
async def test_trace_finish(factory):
    """finish() updates the decision_json and finished_at."""
    repo = SQLiteTraceRepository(factory)
    msg = _make_inbound()
    trace = await repo.start(msg)

    decision = RoutingDecision(
        action=RouteAction.AUTO_SEND,
        reason="All gates passed",
        reason_code="auto_send",
        confidence=None,
    )
    await repo.finish(trace.trace_id, decision)

    fetched = await repo.get(trace.trace_id)
    assert fetched is not None
    assert fetched.decision is not None
    assert fetched.decision.action == RouteAction.AUTO_SEND
    assert fetched.finished_at is not None
