"""End-to-end scenarios.

Real pipeline, real SQLite repositories, real Qdrant index, real outbox, real worker.
The only doubles are the two things that would otherwise cross the network: the LLM
provider and the SMTP transport. Everything between ingestion and the delivery state
is the production code path.

A test here is not allowed to assert that a mock was called. It asserts what the
database ends up holding.
"""

from datetime import UTC, datetime, timedelta

import pytest

from taskflow.adapters.db.repositories import (
    SQLiteAuditRepository,
    SQLiteInboxRepository,
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.domain.enums import Channel, Intent, OutboxState, ReviewState, RouteAction
from taskflow.domain.errors import ConfigError, TransientError
from taskflow.domain.models import (
    Citation,
    ClassificationOutput,
    DocumentChunk,
    DraftOutput,
    InboundMessage,
)
from taskflow.pipeline.orchestrator import Deps, run_pipeline
from taskflow.services.review.service import approve_review, edit_and_approve_review
from taskflow.services.sla.service import sweep_sla_breaches
from taskflow.worker.outbox_worker import process_outbox_batch
from tests.fixtures.fakes import FakeLLMRouter, RecordingChannel

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

TENANT = "taskflow-demo"

KB = [
    DocumentChunk(
        chunk_id="kb-refund:0",
        doc_id="kb-refund",
        ordinal=0,
        title="Refund Policy",
        section_heading="Prorated refunds",
        text=(
            "We issue prorated refunds for unused seat licenses on annual plans upon "
            "request. Credits appear on the next billing statement."
        ),
    ),
    DocumentChunk(
        chunk_id="kb-refund:1",
        doc_id="kb-refund",
        ordinal=1,
        title="Refund Policy",
        section_heading="Windows",
        text="Refund requests must be submitted within 30 days of the charge date.",
    ),
    DocumentChunk(
        chunk_id="kb-tech:0",
        doc_id="kb-tech",
        ordinal=0,
        title="Webhook Configuration",
        section_heading="Timeouts",
        text=(
            "Raise the webhook timeout in the developer portal under Webhook "
            "Configuration. Enable async retries to survive slow downstream endpoints."
        ),
    ),
]


@pytest.fixture
def store():
    q = QdrantVectorStore(location=":memory:")
    q.setup_collection()
    q.ingest(KB, tenant_id=TENANT)
    return q


def build_deps(session_factory, store, router):
    return Deps(
        trace_repo=SQLiteTraceRepository(session_factory),
        llm_router=router,
        vector_store=store,
        review_repo=SQLiteReviewRepository(session_factory),
        outbox_repo=SQLiteOutboxRepository(session_factory),
        processed_repo=SQLiteInboxRepository(session_factory),
    )


def router_for(
    intent: Intent,
    response_text: str,
    citations: list[str],
    tone: str = "apologetic",
    confidence: float = 0.95,
    raises: Exception | None = None,
) -> FakeLLMRouter:
    return FakeLLMRouter(
        raises=raises,
        responses={
            "classification": ClassificationOutput(
                intent=intent, confidence=confidence, reasoning="scenario"
            ),
            "draft": DraftOutput(
                response_text=response_text,
                citations=[Citation(chunk_id=c, doc_title="KB") for c in citations],
                tone=tone,
                complexity="simple",
                draft_confidence=0.9,
            ),
        },
    )


def inbound(body: str, message_id: str, subject: str = "Support request") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        dedupe_key=InboundMessage.make_dedupe_key(Channel.EMAIL, message_id),
        tenant_id=TENANT,
        channel=Channel.EMAIL,
        sender="customer@acme.com",
        subject=subject,
        body_text=body,
        body_redacted=body,
        provider_message_id=message_id,
        received_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------
# Scenario 1 — clean auto-send, all the way to a delivered message
# --------------------------------------------------------------------------


async def test_clean_auto_send_reaches_delivery(session_factory, store):
    """Ingest -> retrieve -> draft -> gates -> auto-send -> outbox -> worker -> SENT."""
    deps = build_deps(
        session_factory,
        store,
        router_for(
            Intent.TECHNICAL,
            "I am sorry for the trouble. Raise the webhook timeout in the developer "
            "portal under Webhook Configuration, and enable async retries to survive "
            "slow downstream endpoints. [kb-tech:0]",
            ["kb-tech:0"],
        ),
    )

    decision = await run_pipeline(
        inbound("Our webhook is returning 504 during peak hours. How do we fix it?", "e2e-1"), deps
    )
    assert decision.action == RouteAction.AUTO_SEND, decision.reason

    channel = RecordingChannel()
    result = await process_outbox_batch(deps.outbox_repo, channel)

    assert result == {"sent": 1, "retried": 0, "dead": 0}
    assert len(channel.sent) == 1
    assert channel.sent[0].recipient == "customer@acme.com"

    # The durable state, not the mock, is the assertion.
    delivered = await deps.outbox_repo.get(channel.sent[0].outbound_id)
    assert delivered.state == OutboxState.SENT
    assert delivered.last_error is None


# --------------------------------------------------------------------------
# Scenario 2 — human review, approved by an operator, then delivered
# --------------------------------------------------------------------------


async def test_human_review_approve_then_deliver(session_factory, store):
    deps = build_deps(
        session_factory,
        store,
        router_for(
            Intent.COMPLAINT,
            "I am sorry about this. Refund requests must be submitted within 30 days "
            "of the charge date. [kb-refund:1]",
            ["kb-refund:1"],
        ),
    )
    audit_repo = SQLiteAuditRepository(session_factory)

    decision = await run_pipeline(
        inbound("This is unacceptable, I was charged twice again.", "e2e-2"), deps
    )
    assert decision.action == RouteAction.HUMAN_REVIEW
    assert decision.reason_code == "G4_intent_allows_auto"

    pending = await deps.review_repo.pending()
    assert len(pending) == 1
    item = pending[0]

    # A complaint's SLA comes from config (60 min), not a hardcoded four hours.
    minutes = (item.sla_deadline - item.created_at).total_seconds() / 60
    assert minutes == pytest.approx(60, abs=1)

    assert await approve_review(
        deps.review_repo, deps.outbox_repo, item.review_id, operator="alice", audit_repo=audit_repo
    )
    assert (await deps.review_repo.get(item.review_id)).state == ReviewState.APPROVED

    trail = await audit_repo.for_entity(item.review_id)
    assert [(e["actor"], e["action"]) for e in trail] == [("alice", "review.approved")]

    channel = RecordingChannel()
    assert await process_outbox_batch(deps.outbox_repo, channel) == {
        "sent": 1,
        "retried": 0,
        "dead": 0,
    }
    assert channel.sent[0].body_text == item.draft.response_text


async def test_human_review_edit_changes_what_is_delivered(session_factory, store):
    deps = build_deps(
        session_factory,
        store,
        router_for(Intent.COMPLAINT, "Sorry about that. [kb-refund:1]", ["kb-refund:1"]),
    )
    await run_pipeline(inbound("Terrible experience, fix it.", "e2e-3"), deps)
    item = (await deps.review_repo.pending())[0]

    edited = "Thank you for your patience — I have escalated this to our billing team."
    assert await edit_and_approve_review(
        deps.review_repo, deps.outbox_repo, item.review_id, edited_text=edited, operator="bob"
    )

    channel = RecordingChannel()
    await process_outbox_batch(deps.outbox_repo, channel)
    assert channel.sent[0].body_text == edited


async def test_rejected_review_sends_nothing(session_factory, store):
    from taskflow.services.review.service import reject_review

    deps = build_deps(
        session_factory,
        store,
        router_for(Intent.COMPLAINT, "Sorry about that. [kb-refund:1]", ["kb-refund:1"]),
    )
    await run_pipeline(inbound("Awful. Just awful.", "e2e-4"), deps)
    item = (await deps.review_repo.pending())[0]

    assert await reject_review(deps.review_repo, item.review_id, operator="carol", reason="spam")
    assert (await deps.review_repo.get(item.review_id)).state == ReviewState.REJECTED

    channel = RecordingChannel()
    assert await process_outbox_batch(deps.outbox_repo, channel) == {
        "sent": 0,
        "retried": 0,
        "dead": 0,
    }
    assert channel.sent == []


# --------------------------------------------------------------------------
# Scenario 3 — every provider fails
# --------------------------------------------------------------------------


async def test_all_providers_failing_never_auto_sends(session_factory, store):
    """The template fallback keeps the message flowing, and the gates catch it.

    The reply that reaches a customer must never be the fallback stub.
    """
    from taskflow.domain.errors import AllProvidersFailed

    deps = build_deps(
        session_factory, store, FakeLLMRouter(raises=AllProvidersFailed("every provider is down"))
    )

    decision = await run_pipeline(
        inbound("I need a prorated refund for 3 unused seats.", "e2e-5"), deps
    )

    assert decision.action == RouteAction.HUMAN_REVIEW
    assert len(await deps.review_repo.pending()) == 1
    assert await deps.outbox_repo.claim(limit=10) == []


# --------------------------------------------------------------------------
# Scenario 4 — delivery failure is never recorded as success
# --------------------------------------------------------------------------


async def test_transport_failure_is_retried_then_dead_never_sent(session_factory, store):
    """BRD R-5. `deliver_message` used to fall through to `return True`, so a failed
    SMTP send was marked SENT and the reply was lost silently."""
    deps = build_deps(
        session_factory,
        store,
        router_for(
            Intent.TECHNICAL,
            "I am sorry for the trouble. Raise the webhook timeout in the developer "
            "portal under Webhook Configuration. [kb-tech:0]",
            ["kb-tech:0"],
        ),
    )
    await run_pipeline(inbound("Webhook 504 during peak hours, please advise.", "e2e-6"), deps)

    channel = RecordingChannel(fail_with=TransientError("smtp unreachable"))
    outbound_id = (await deps.outbox_repo.claim(limit=1))[0].outbound_id
    await deps.outbox_repo.mark(outbound_id, OutboxState.FAILED)  # return it to QUEUED

    states: list[OutboxState] = []
    for _ in range(4):
        await process_outbox_batch(deps.outbox_repo, channel)
        row = await deps.outbox_repo.get(outbound_id)
        states.append(row.state)
        if row.state is OutboxState.DEAD:
            break

    final = await deps.outbox_repo.get(outbound_id)
    assert OutboxState.SENT not in states, "a failed delivery was reported as sent"
    assert final.state is OutboxState.DEAD
    assert final.retry_count >= 1
    assert "smtp unreachable" in final.last_error
    assert channel.sent == []


async def test_transport_recovers_within_retry_budget(session_factory, store):
    """A transient failure that clears is delivered, not abandoned."""
    deps = build_deps(
        session_factory,
        store,
        router_for(
            Intent.TECHNICAL,
            "I am sorry for the trouble. Raise the webhook timeout in the developer "
            "portal under Webhook Configuration. [kb-tech:0]",
            ["kb-tech:0"],
        ),
    )
    await run_pipeline(inbound("Webhook 504 during peak hours, please advise.", "e2e-7"), deps)

    channel = RecordingChannel(fail_with=TransientError("blip"), fail_times=1)
    await process_outbox_batch(deps.outbox_repo, channel)  # fails, requeues
    await process_outbox_batch(deps.outbox_repo, channel)  # succeeds

    assert len(channel.sent) == 1
    delivered = await deps.outbox_repo.get(channel.sent[0].outbound_id)
    assert delivered.state is OutboxState.SENT
    assert delivered.retry_count == 1


async def test_misconfigured_transport_is_dead_immediately(session_factory, store):
    """A ConfigError will not fix itself on retry, so it skips the retry budget."""
    deps = build_deps(
        session_factory,
        store,
        router_for(
            Intent.TECHNICAL,
            "I am sorry for the trouble. Raise the webhook timeout in the developer "
            "portal under Webhook Configuration. [kb-tech:0]",
            ["kb-tech:0"],
        ),
    )
    await run_pipeline(inbound("Webhook 504 during peak hours, please advise.", "e2e-8"), deps)

    channel = RecordingChannel(fail_with=ConfigError("GMAIL_APP_PASSWORD unset"))
    result = await process_outbox_batch(deps.outbox_repo, channel)

    assert result == {"sent": 0, "retried": 0, "dead": 1}


# --------------------------------------------------------------------------
# Scenario 5 — duplicate inbound
# --------------------------------------------------------------------------


async def test_duplicate_inbound_produces_one_reply(session_factory, store):
    router = router_for(
        Intent.TECHNICAL,
        "I am sorry for the trouble. Raise the webhook timeout in the developer "
        "portal under Webhook Configuration. [kb-tech:0]",
        ["kb-tech:0"],
    )
    deps = build_deps(session_factory, store, router)
    msg = inbound("Webhook 504 during peak hours, please advise.", "e2e-9")

    first = await run_pipeline(msg, deps)
    second = await run_pipeline(msg, deps)

    assert first.action == RouteAction.AUTO_SEND
    assert second.action == RouteAction.DROPPED_DUPLICATE

    channel = RecordingChannel()
    await process_outbox_batch(deps.outbox_repo, channel)
    assert len(channel.sent) == 1, "the customer received the same reply twice"
    assert len(router.prompts_for("draft")) == 1, "the duplicate still cost an LLM call"


async def test_idempotency_key_blocks_a_second_identical_enqueue(session_factory, store):
    """Second line of send-once defence, below deduplication."""
    from taskflow.domain.models import OutboundMessage

    repo = SQLiteOutboxRepository(session_factory)
    msg = OutboundMessage(
        outbound_id="out-a",
        conversation_id="conv-1",
        tenant_id=TENANT,
        channel=Channel.EMAIL,
        recipient="customer@acme.com",
        subject="Re: hello",
        body_text="the same body",
        idempotency_key=OutboundMessage.make_idempotency_key("conv-1", "the same body"),
    )
    twin = msg.model_copy(update={"outbound_id": "out-b"})

    assert await repo.enqueue(msg) is True
    assert await repo.enqueue(twin) is False
    assert len(await repo.claim(limit=10)) == 1


# --------------------------------------------------------------------------
# Scenario 6 — PII never leaves the process unredacted
# --------------------------------------------------------------------------


async def test_pii_is_redacted_everywhere_it_could_escape(session_factory, store):
    """Card numbers must not reach the provider prompt, the review row, or the trace."""
    router = router_for(
        Intent.COMPLAINT, "I am sorry about the duplicate charge. [kb-refund:1]", ["kb-refund:1"]
    )
    deps = build_deps(session_factory, store, router)

    card = "4111222233334444"
    await run_pipeline(inbound(f"You charged card {card} twice! Awful.", "e2e-10"), deps)

    for prompt in router.calls:
        assert card not in prompt["user"], f"{prompt['purpose']} prompt leaked the card number"

    item = (await deps.review_repo.pending())[0]
    assert card not in (item.inbound_body or "")

    trace = (await deps.trace_repo.recent(limit=1))[0]
    assert card not in trace.model_dump_json()


# --------------------------------------------------------------------------
# Scenario 7 — SLA breach sweep
# --------------------------------------------------------------------------


async def test_sla_breach_escalates_an_overdue_review(session_factory, store):
    deps = build_deps(
        session_factory,
        store,
        router_for(Intent.COMPLAINT, "Sorry about that. [kb-refund:1]", ["kb-refund:1"]),
    )
    await run_pipeline(inbound("Dreadful service.", "e2e-11"), deps)
    item = (await deps.review_repo.pending())[0]

    # Nothing is due yet.
    assert await sweep_sla_breaches(deps.review_repo, now=item.created_at) == []

    later = item.sla_deadline + timedelta(minutes=5)
    escalated = await sweep_sla_breaches(deps.review_repo, now=later)

    assert [i.review_id for i in escalated] == [item.review_id]
    assert (await deps.review_repo.get(item.review_id)).state == ReviewState.ESCALATED
    assert await deps.review_repo.pending() == []

    # A second sweep must not re-escalate what it already handled.
    assert await sweep_sla_breaches(deps.review_repo, now=later) == []


# --------------------------------------------------------------------------
# Scenario 8 — the holding template is never auto-sent
# --------------------------------------------------------------------------


async def test_template_fallback_is_never_auto_sent(session_factory, store):
    """Gate G8.

    Discovered while verifying `local_only` mode against a real Ollama instance.
    When every provider failed, drafting produced a holding template that cited the
    first retrieved chunk it had never used. That fabricated citation scored a
    perfect 1.00 for coverage, lifting a content-free stub to 0.862 — above the
    0.70 bar for its intent. Only an unrelated policy veto stopped it from being
    auto-sent to the customer.
    """
    from taskflow.domain.errors import AllProvidersFailed

    deps = build_deps(
        session_factory,
        store,
        FakeLLMRouter(
            raises=AllProvidersFailed("every provider is down"),
        ),
    )

    decision = await run_pipeline(inbound("How do I raise my webhook timeout?", "e2e-12"), deps)

    assert decision.action == RouteAction.HUMAN_REVIEW
    breakdown = decision.confidence
    g8 = next(g for g in breakdown.gates if g.gate_id.value == "G8_draft_model_generated")
    assert not g8.passed, "a template fallback passed the draft-source gate"

    item = (await deps.review_repo.pending())[0]
    assert item.draft.citations == [], "the holding template fabricated a citation"
    assert await deps.outbox_repo.claim(limit=10) == []


async def test_template_fallback_would_otherwise_clear_the_bar(session_factory, store):
    """The gate is load-bearing, not decoration: without G8 the same draft scores
    above its threshold."""
    from taskflow.domain.errors import AllProvidersFailed

    deps = build_deps(
        session_factory,
        store,
        FakeLLMRouter(raises=AllProvidersFailed("down")),
    )
    decision = await run_pipeline(inbound("How do I raise my webhook timeout?", "e2e-13"), deps)

    failed = {g.gate_id.value for g in decision.confidence.failed_gates}
    assert "G8_draft_model_generated" in failed
