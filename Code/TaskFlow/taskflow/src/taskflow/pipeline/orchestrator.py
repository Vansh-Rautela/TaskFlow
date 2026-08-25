"""The Pipeline Orchestrator.

This is the walking skeleton from Phase P2.
It defines the strict sequential path of the message through the system.
No hidden event buses or magics. Top-to-bottom explicitness.
"""

import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from taskflow.adapters.llm.router import ProviderRouter
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.config.settings import thresholds_config
from taskflow.domain.models import InboundMessage, RoutingDecision
from taskflow.pipeline.state import PipelineState
from taskflow.ports.repositories import OutboxRepository, ReviewRepository, TraceRepository
from taskflow.ports.vector_store import VectorStore
from taskflow.services.classify.service import classify_intent
from taskflow.services.confidence.gates import evaluate_gates
from taskflow.services.confidence.scorer import compute, decide
from taskflow.services.draft.service import generate_draft
from taskflow.services.preprocess.service import preprocess_message_text
from taskflow.services.retrieve.service import retrieve_context
from taskflow.services.validate.runner import run_validators


@dataclass
class Deps:
    """Dependency injection container for the pipeline scope."""

    trace_repo: TraceRepository
    llm_router: ProviderRouter
    vector_store: VectorStore | None = None
    review_repo: ReviewRepository | None = None
    outbox_repo: OutboxRepository | None = None


async def _time_stage[T](
    state: PipelineState, deps: Deps, stage: str, coro: Coroutine[Any, Any, T]
) -> tuple[PipelineState, T]:
    """Helper to trace how long a stage took."""
    start = time.perf_counter_ns()
    result = await coro
    elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000

    # We log an event to the trace immediately
    await deps.trace_repo.event(
        trace_id=state.trace.trace_id,
        stage=stage,
        payload={},  # We will refine payloads later
        elapsed_ms=elapsed_ms,
    )
    return state, result


async def run_pipeline(msg: InboundMessage, deps: Deps) -> RoutingDecision:
    """Execute the full AI support agent pipeline path."""

    # 0. Initialize Trace & State and Preprocess PII
    redacted_body = preprocess_message_text(msg.body_text)
    redacted_msg = msg.model_copy(update={"body_redacted": redacted_body})
    trace = await deps.trace_repo.start(redacted_msg)
    state = PipelineState(message=redacted_msg, trace=trace)

    # 1. Classify
    state, (intent, intent_confidence, classify_call) = await _time_stage(
        state, deps, "classify", classify_intent(redacted_body, router=deps.llm_router)
    )
    if classify_call:
        state.trace.llm_calls.append(classify_call)
    state = state.replace(intent=intent, intent_confidence=intent_confidence)

    # 2. Retrieve
    store = deps.vector_store or QdrantVectorStore()
    state, retrieval = await _time_stage(
        state, deps, "retrieve", retrieve_context(redacted_body, msg.tenant_id, vector_store=store)
    )
    state = state.replace(retrieval=retrieval)

    # 3. Draft
    assert state.intent is not None
    assert state.retrieval is not None
    state, (draft, draft_call) = await _time_stage(
        state, deps, "draft", generate_draft(msg, state.intent, state.retrieval, deps.llm_router)
    )
    if draft_call:
        state.trace.llm_calls.append(draft_call)
    state = state.replace(draft=draft)

    # 4. Validate & Policy Check
    assert state.draft is not None
    state, validators = await _time_stage(
        state, deps, "validate", run_validators(state.draft, state.retrieval)
    )
    state = state.replace(validators=tuple(validators))

    # Evaluate Policy Rules
    from taskflow.services.policy.service import evaluate_policies
    violations = evaluate_policies(msg, state.draft, state.intent)

    # 5. Evaluate Gates (Safety Vetoes)
    assert state.intent is not None
    citations_val = next((v for v in state.validators if v.validator_name == "citations"), None)
    citations_resolve = citations_val.passed if citations_val else True
    suspicious_context = state.retrieval.suspicious_context if state.retrieval else False

    state, gates_result = await _time_stage(
        state,
        deps,
        "evaluate_gates",
        asyncio_wrap(
            evaluate_gates(
                intent=state.intent,
                intent_confidence=state.intent_confidence,
                abstain_threshold=thresholds_config()["gates"]["abstain_threshold"],
                validators=list(state.validators),
                violations=violations,
                citations_resolve=citations_resolve,
                suspicious_context=suspicious_context,
            )
        ),
    )

    # 6. Score & Decide (Quality math)
    grounding_val = next((v for v in state.validators if v.validator_name == "grounding"), None)
    grounding_score = grounding_val.score if grounding_val else 1.0

    weights = thresholds_config()["weights"]
    signals = {
        "intent_confidence": state.intent_confidence,
        "retrieval_relevance": 1.0 if (state.retrieval and state.retrieval.sufficient) else 0.0,
        "citation_coverage": 1.0 if (citations_val and citations_val.passed) else 0.0,
        "grounding_entailment": grounding_score,
        "tone_alignment": 1.0,
    }

    breakdown = compute(
        gates=gates_result,
        signals=signals,
        weights=weights,
        threshold=thresholds_config()["thresholds"]["general"],
        draft_confidence=state.draft.draft_confidence if state.draft else 0.0,
    )

    decision = decide(breakdown)
    state = state.replace(confidence=breakdown, decision=decision)

    # Auto-enqueue for delivery or human review
    import uuid
    from datetime import UTC, datetime, timedelta

    from taskflow.domain.enums import ReviewState, RouteAction
    from taskflow.domain.models import OutboundMessage, ReviewItem

    if decision.action == RouteAction.AUTO_SEND and deps.outbox_repo and state.draft:
        outbound_id = f"out-{uuid.uuid4().hex[:12]}"
        idempotency_key = OutboundMessage.make_idempotency_key(
            state.trace.conversation_id, state.draft.response_text
        )
        outbound_msg = OutboundMessage(
            outbound_id=outbound_id,
            conversation_id=state.trace.conversation_id,
            tenant_id=msg.tenant_id,
            channel=msg.channel,
            recipient=msg.sender,
            subject=f"Re: {msg.subject}" if msg.subject else "Re: TaskFlow Support Request",
            body_text=state.draft.response_text,
            idempotency_key=idempotency_key,
        )
        await deps.outbox_repo.enqueue(outbound_msg)

    elif decision.action == RouteAction.HUMAN_REVIEW and deps.review_repo:
        now = datetime.now(UTC)
        review_item = ReviewItem(
            review_id=f"rev-{uuid.uuid4().hex[:12]}",
            trace_id=state.trace.trace_id,
            conversation_id=state.trace.conversation_id,
            tenant_id=msg.tenant_id,
            state=ReviewState.PENDING,
            draft=state.draft,
            decision=decision,
            created_at=now,
            sla_deadline=now + timedelta(hours=4),
            sender_email=msg.sender,
            inbound_body=msg.body_text,
        )
        await deps.review_repo.create(review_item)

        try:
            from taskflow.services.alert.service import dispatch_alert
            await dispatch_alert(review_item)
        except Exception as err:
            pass

    # 7. Finalize trace
    updated_trace = state.trace.model_copy(
        update={
            "intent": state.intent,
            "intent_confidence": state.intent_confidence,
            "retrieval": state.retrieval,
            "draft": state.draft,
            "validators": list(state.validators),
            "confidence": state.confidence,
            "decision": decision,
            "llm_calls": state.trace.llm_calls,
        }
    )
    state = state.replace(trace=updated_trace)
    await deps.trace_repo.update(state.trace)
    await deps.trace_repo.finish(state.trace.trace_id, decision)

    return decision


async def asyncio_wrap(result):
    return result
