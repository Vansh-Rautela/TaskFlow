"""Orchestrator tests.

These cover what the pipeline itself is responsible for — the order of stages, what
each stage is handed, and what it records — as distinct from the correctness of any
one service, which its own unit tests cover.
"""

from datetime import UTC, datetime

import pytest

from taskflow.adapters.db.repositories import (
    SQLiteInboxRepository,
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.config.settings import thresholds_config
from taskflow.domain.enums import Channel, Intent, RouteAction
from taskflow.domain.models import (
    Citation,
    ClassificationOutput,
    DraftOutput,
    InboundMessage,
)
from taskflow.pipeline.orchestrator import Deps, run_pipeline
from tests.fixtures.fakes import FakeLLMRouter, FakeReranker, FakeVectorStore, chunk

GROUNDING_TEXT = (
    "Prorated refunds are issued for unused seat licenses on annual plans upon request."
)


def make_message(body: str, message_id: str = "msg-1", **overrides) -> InboundMessage:
    defaults = dict(
        message_id=message_id,
        dedupe_key=InboundMessage.make_dedupe_key(Channel.EMAIL, message_id),
        tenant_id="test",
        channel=Channel.EMAIL,
        sender="customer@example.com",
        subject="Support request",
        body_text=body,
        body_redacted=body,
        provider_message_id=message_id,
        received_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return InboundMessage(**defaults)


def make_router(
    intent: Intent = Intent.BILLING,
    confidence: float = 0.95,
    response_text: str = f"I am sorry for the confusion. {GROUNDING_TEXT} [kb-1:0]",
    tone: str = "apologetic",
) -> FakeLLMRouter:
    return FakeLLMRouter(
        responses={
            "classification": ClassificationOutput(
                intent=intent, confidence=confidence, reasoning="test"
            ),
            "draft": DraftOutput(
                response_text=response_text,
                citations=[Citation(chunk_id="kb-1:0", doc_title="Refund Policy")],
                tone=tone,
                complexity="simple",
                draft_confidence=0.9,
            ),
        }
    )


def make_store() -> FakeVectorStore:
    return FakeVectorStore(
        chunks=[
            chunk("kb-1:0", GROUNDING_TEXT, title="Refund Policy"),
            chunk("kb-1:1", "Refund requests must be submitted within 30 days.", title="Refunds"),
        ],
        scores=[0.85, 0.60],
    )


@pytest.fixture
def deps(session_factory):
    return Deps(
        trace_repo=SQLiteTraceRepository(session_factory),
        llm_router=make_router(),
        vector_store=make_store(),
        review_repo=SQLiteReviewRepository(session_factory),
        outbox_repo=SQLiteOutboxRepository(session_factory),
        processed_repo=SQLiteInboxRepository(session_factory),
    )


@pytest.mark.asyncio
async def test_clean_draft_auto_sends(deps):
    """The control: gates clear, the score clears the intent's bar, it sends."""
    decision = await run_pipeline(make_message("I was double charged on my invoice"), deps)

    assert decision.action == RouteAction.AUTO_SEND, decision.reason
    assert decision.reason_code == "auto_send"

    claimed = await deps.outbox_repo.claim(limit=10)
    assert len(claimed) == 1
    assert claimed[0].recipient == "customer@example.com"


@pytest.mark.asyncio
async def test_complaint_escalates(deps):
    """Gate G4 makes `complaint` permanently ineligible for auto-send."""
    deps.llm_router = make_router(intent=Intent.COMPLAINT, confidence=0.98)

    decision = await run_pipeline(make_message("Your service is terrible"), deps)

    assert decision.action == RouteAction.HUMAN_REVIEW
    assert decision.reason_code == "G4_intent_allows_auto"
    assert len(await deps.review_repo.pending()) == 1


@pytest.mark.asyncio
async def test_threshold_is_selected_per_intent(session_factory):
    """BRD FR-8.3 / R-3.

    The same draft quality is judged against a different bar per intent. The
    orchestrator used to apply `thresholds.general` (0.70) to everything.
    """
    configured = thresholds_config()["thresholds"]
    seen: dict[str, float] = {}

    for intent in (Intent.GENERAL, Intent.BILLING, Intent.REFUND):
        deps = Deps(
            trace_repo=SQLiteTraceRepository(session_factory),
            llm_router=make_router(intent=intent),
            vector_store=make_store(),
            review_repo=SQLiteReviewRepository(session_factory),
            outbox_repo=SQLiteOutboxRepository(session_factory),
        )
        decision = await run_pipeline(make_message("a question", message_id=f"m-{intent}"), deps)
        seen[intent.value] = decision.confidence.threshold

    assert seen == {
        "general": configured["general"],
        "billing": configured["billing"],
        "refund": configured["refund"],
    }
    assert len(set(seen.values())) > 1, "all intents shared one threshold"


@pytest.mark.asyncio
async def test_drafting_prompt_never_receives_unredacted_pii(deps):
    """BRD R-1. The drafting prompt used to interpolate `body_text`, so a card number
    in a customer's mail was sent verbatim to the provider."""
    router = make_router()
    deps.llm_router = router

    await run_pipeline(
        make_message("You charged card 4111222233334444 and SSN 123-45-6789 twice"), deps
    )

    prompts = router.prompts_for("draft") + router.prompts_for("classification")
    assert prompts, "no prompt was captured"
    for prompt in prompts:
        assert "4111222233334444" not in prompt
        assert "123-45-6789" not in prompt
        assert "[REDACTED_CREDIT_CARD]" in prompt or "[REDACTED_SSN]" in prompt


@pytest.mark.asyncio
async def test_duplicate_message_is_dropped_before_any_work(deps):
    """BRD FR-1.4. The same inbound event twice must not produce two replies."""
    msg = make_message("I was double charged on my invoice", message_id="dup-1")

    first = await run_pipeline(msg, deps)
    second = await run_pipeline(msg, deps)

    assert first.action == RouteAction.AUTO_SEND
    assert second.action == RouteAction.DROPPED_DUPLICATE
    assert second.reason_code == "duplicate"

    # One reply, not two — and the duplicate never reached the model.
    assert len(await deps.outbox_repo.claim(limit=10)) == 1
    assert len(deps.llm_router.prompts_for("draft")) == 1


@pytest.mark.asyncio
async def test_trace_events_carry_real_payloads(deps):
    """BRD BO-2 / the trace-payload rule (docs/08_OBSERVABILITY.md).

    Stage events used to persist `payload={}`, so a trace recorded how long each
    stage took and nothing about what it decided.
    """
    from sqlalchemy import select

    from taskflow.adapters.db.orm import TraceEventRow

    await run_pipeline(make_message("I was double charged on my invoice"), deps)

    async with deps.trace_repo._factory() as session:
        rows = (await session.execute(select(TraceEventRow))).scalars().all()

    by_stage = {r.stage: r.payload for r in rows}
    assert {
        "classify",
        "retrieve",
        "draft",
        "validate",
        "policy",
        "evaluate_gates",
        "decide",
    } <= set(by_stage)
    assert all(p for p in by_stage.values()), (
        f"empty payloads: {[s for s, p in by_stage.items() if not p]}"
    )

    assert by_stage["classify"]["intent"] == "billing"
    assert by_stage["retrieve"]["chunk_ids"] == ["kb-1:0", "kb-1:1"]
    assert by_stage["draft"]["citations"] == ["kb-1:0"]
    assert by_stage["decide"]["action"] == "auto_send"
    assert by_stage["decide"]["threshold_source"] == "thresholds.billing"
    assert set(by_stage["decide"]["signals"]) == {
        "citation_coverage",
        "grounding_entailment",
        "retrieval_relevance",
        "intent_confidence",
        "tone_alignment",
    }


@pytest.mark.asyncio
async def test_retrieval_relevance_is_the_measured_score_not_a_boolean(deps):
    """The 0.20-weighted retrieval signal must vary with the retrieval.

    It used to be `1.0 if sufficient else 0.0`, and because every retrieval was
    declared sufficient it contributed a constant 0.20 to every quality score — a
    weighted signal that measured nothing. It is now the reranker's relevance for
    the best chunk.
    """
    from sqlalchemy import select

    from taskflow.adapters.db.orm import TraceEventRow

    deps.reranker = FakeReranker({"kb-1:0": 0.73, "kb-1:1": 0.41})

    await run_pipeline(make_message("I was double charged on my invoice"), deps)

    async with deps.trace_repo._factory() as session:
        rows = (await session.execute(select(TraceEventRow))).scalars().all()
    by_stage = {r.stage: r.payload for r in rows}

    assert by_stage["decide"]["signals"]["retrieval_relevance"] == 0.73
    assert by_stage["retrieve"]["top_score"] == 0.73


@pytest.mark.asyncio
async def test_a_retrieval_gap_lowers_the_score_instead_of_being_ignored(deps):
    """Weak retrieval must cost the message its auto-send, not pass at a constant."""
    deps.reranker = FakeReranker({"kb-1:0": 0.02, "kb-1:1": 0.01})

    decision = await run_pipeline(make_message("I was double charged on my invoice"), deps)

    assert decision.action == RouteAction.HUMAN_REVIEW
    assert decision.confidence is not None
    assert decision.confidence.retrieval_relevance == 0.02


@pytest.mark.asyncio
async def test_review_item_stores_redacted_body(deps):
    """The review body is forwarded to Telegram/webhook by the alerter, so what
    is persisted on the item must already be redacted."""
    deps.llm_router = make_router(intent=Intent.COMPLAINT)

    await run_pipeline(make_message("Awful service. My card 4111222233334444 was charged."), deps)

    item = (await deps.review_repo.pending())[0]
    assert "4111222233334444" not in item.inbound_body
    assert "[REDACTED_CREDIT_CARD]" in item.inbound_body
