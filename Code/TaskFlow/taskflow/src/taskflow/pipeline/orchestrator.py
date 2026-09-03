"""The pipeline orchestrator.

One strict sequential path from inbound message to routing decision. No event bus,
no framework — the order below is the order the code runs in, top to bottom.

    dedupe -> redact -> classify -> retrieve -> draft -> validate -> policy
           -> gates -> score -> decide -> (outbox | review + alert) -> trace

Dependencies arrive as ports via `Deps`. This module imports no adapter.
"""

import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from taskflow.config.settings import sla_minutes_for, threshold_for, thresholds_config
from taskflow.domain.enums import ReviewState, RouteAction
from taskflow.domain.models import (
    InboundMessage,
    OutboundMessage,
    ReviewItem,
    RoutingDecision,
)
from taskflow.pipeline.state import PipelineState
from taskflow.ports.pii import PIIDetector
from taskflow.ports.repositories import (
    OutboxRepository,
    ProcessedMessageRepository,
    ReviewRepository,
    TraceRepository,
)
from taskflow.ports.reranker import Reranker
from taskflow.ports.vector_store import VectorStore
from taskflow.services.alert.service import dispatch_alert
from taskflow.services.classify.service import classify_intent
from taskflow.services.confidence.gates import evaluate_gates
from taskflow.services.confidence.scorer import compute, decide
from taskflow.services.confidence.tone import score_tone
from taskflow.services.draft.service import generate_draft
from taskflow.services.policy.service import evaluate_policies
from taskflow.services.preprocess.service import preprocess_message_text
from taskflow.services.retrieve.service import retrieve_context
from taskflow.services.validate.runner import run_validators

logger = structlog.get_logger()


@dataclass
class Deps:
    """Everything the pipeline needs, as ports. Wired at startup."""

    trace_repo: TraceRepository
    llm_router: Any  # ports.llm.LLMRouter — Protocol, kept loose for test doubles
    vector_store: VectorStore
    reranker: Reranker | None = None
    review_repo: ReviewRepository | None = None
    outbox_repo: OutboxRepository | None = None
    processed_repo: ProcessedMessageRepository | None = None
    # None defaults to services.pii.RegexPIIDetector at each call site — see
    # preprocess.service and validate.pii_leak. bootstrap.py wires PresidioDetector.
    pii_detector: PIIDetector | None = None


async def _stage[T](
    deps: Deps,
    trace_id: str,
    stage: str,
    awaitable: Awaitable[T],
    payload: Any = None,
) -> T:
    """Run one stage, time it, and persist a trace event describing what it produced.

    `payload` is a callable taking the stage result and returning a JSON-safe dict.
    Events carry real content: an event with an empty payload cannot explain a
    decision, and claiming reconstructability from timings alone would be a lie.
    """
    start = time.perf_counter_ns()
    result = await awaitable
    elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000

    event_payload: dict[str, Any] = {}
    if payload is not None:
        try:
            event_payload = payload(result)
        except Exception as err:  # a broken summariser must not fail the pipeline
            logger.warning("trace_payload_build_failed", stage=stage, error=str(err))
            event_payload = {"payload_error": str(err)}

    await deps.trace_repo.event(
        trace_id=trace_id, stage=stage, payload=event_payload, elapsed_ms=elapsed_ms
    )
    return result


async def run_pipeline(msg: InboundMessage, deps: Deps) -> RoutingDecision:
    """Execute the full support-agent pipeline and return the routing decision."""

    # 0. Deduplicate before doing any work. The UNIQUE constraint on inbox.dedupe_key
    #    is the enforcement; this is the call that actually consults it (BRD FR-1.4).
    if deps.processed_repo is not None:
        claimed = await deps.processed_repo.try_claim(msg.dedupe_key)
        if not claimed:
            logger.info("inbound_duplicate_dropped", dedupe_key=msg.dedupe_key)
            return RoutingDecision(
                action=RouteAction.DROPPED_DUPLICATE,
                reason=f"dedupe_key {msg.dedupe_key} has already been processed",
                reason_code="duplicate",
            )

    # 1. Redact PII. Everything downstream that reaches a provider uses body_redacted.
    redacted_body = preprocess_message_text(msg.body_text, deps.pii_detector)
    msg = msg.model_copy(update={"body_redacted": redacted_body})

    trace = await deps.trace_repo.start(msg)
    state = PipelineState(message=msg, trace=trace)
    tid = trace.trace_id

    # 2. Classify
    intent, intent_confidence, classify_call = await _stage(
        deps,
        tid,
        "classify",
        classify_intent(redacted_body, router=deps.llm_router),
        lambda r: {
            "intent": r[0].value,
            "confidence": round(r[1], 4),
            "source": "llm" if r[2] else "keyword_fallback",
        },
    )
    if classify_call:
        state.trace.llm_calls.append(classify_call)
    state = state.replace(intent=intent, intent_confidence=intent_confidence)

    # 3. Retrieve
    retrieval = await _stage(
        deps,
        tid,
        "retrieve",
        retrieve_context(
            redacted_body,
            msg.tenant_id,
            vector_store=deps.vector_store,
            reranker=deps.reranker,
        ),
        lambda r: {
            "query_used": r.query_used,
            "chunk_ids": [c.chunk.chunk_id for c in r.chunks],
            "top_score": round(r.top_score, 4),
            "support_count": r.support_count,
            "sufficient": r.sufficient,
            "gap_reason": r.gap_reason,
            "suspicious_context": r.suspicious_context,
        },
    )
    state = state.replace(retrieval=retrieval)

    # 4. Draft
    draft, draft_call = await _stage(
        deps,
        tid,
        "draft",
        generate_draft(msg, intent, retrieval, deps.llm_router),
        lambda r: {
            "chars": len(r[0].response_text),
            "tone": r[0].tone,
            "complexity": r[0].complexity,
            "citations": [c.chunk_id for c in r[0].citations],
            "source": "llm" if r[1] else "template_fallback",
        },
    )
    if draft_call:
        state.trace.llm_calls.append(draft_call)
    state = state.replace(draft=draft)

    # 5. Validate
    validators = await _stage(
        deps,
        tid,
        "validate",
        run_validators(
            draft,
            retrieval,
            timeout_s=float(thresholds_config()["validator_timeout_s"]),
            pii_detector=deps.pii_detector,
        ),
        lambda r: {
            v.validator_name: {
                "passed": v.passed,
                "score": v.score,
                "reason": v.reason,
                "errored": v.errored,
            }
            for v in r
        },
    )
    state = state.replace(validators=tuple(validators))

    # 6. Policy. Runs locally over the raw body — a legal threat has to be detected in
    #    the words the customer actually used, and nothing here leaves the process.
    violations = evaluate_policies(msg, draft, intent)
    await deps.trace_repo.event(
        trace_id=tid,
        stage="policy",
        payload={
            "violations": [
                {"rule_id": v.rule_id, "severity": v.severity.value, "description": v.description}
                for v in violations
            ]
        },
        elapsed_ms=0,
    )

    # 7. Gates (safety vetoes)
    citations_val = next((v for v in validators if v.validator_name == "citations"), None)
    gates_result = evaluate_gates(
        intent=intent,
        intent_confidence=intent_confidence,
        abstain_threshold=float(thresholds_config()["gates"]["abstain_threshold"]),
        validators=list(validators),
        violations=violations,
        citations_resolve=citations_val.passed if citations_val else True,
        suspicious_context=retrieval.suspicious_context,
        # generate_draft returns no LLMCall when it fell back to the holding template.
        draft_is_model_generated=draft_call is not None,
    )
    await deps.trace_repo.event(
        trace_id=tid,
        stage="evaluate_gates",
        payload={
            "gates": [
                {"gate_id": g.gate_id.value, "passed": g.passed, "reason": g.reason}
                for g in gates_result
            ]
        },
        elapsed_ms=0,
    )

    # 8. Score. Threshold is selected per intent (BRD FR-8.3), not universally.
    grounding_val = next((v for v in validators if v.validator_name == "grounding"), None)
    tone_score, tone_reason = score_tone(draft, intent, violations)

    signals = {
        "intent_confidence": intent_confidence,
        # The reranker's calibrated relevance for the best chunk, not a boolean derived
        # from it. A weighted signal that only ever reads 0.0 or 1.0 is a gate wearing
        # a weight, and for as long as every retrieval passed it was a constant.
        "retrieval_relevance": retrieval.top_score,
        "citation_coverage": citations_val.score if citations_val else 0.0,
        "grounding_entailment": grounding_val.score if grounding_val else 0.0,
        "tone_alignment": tone_score,
    }
    threshold = threshold_for(intent.value)

    breakdown = compute(
        gates=gates_result,
        signals=signals,
        weights=thresholds_config()["weights"],
        threshold=threshold,
        draft_confidence=draft.draft_confidence,
    )
    decision = decide(breakdown)
    state = state.replace(confidence=breakdown, decision=decision)

    await deps.trace_repo.event(
        trace_id=tid,
        stage="decide",
        payload={
            "action": decision.action.value,
            "reason_code": decision.reason_code,
            "reason": decision.reason,
            "score": round(breakdown.score, 4),
            "threshold": threshold,
            "threshold_source": f"thresholds.{intent.value}",
            "signals": {k: round(v, 4) for k, v in signals.items()},
            "tone_reason": tone_reason,
        },
        elapsed_ms=0,
    )

    # 9. Act on the decision.
    if decision.action == RouteAction.AUTO_SEND:
        await _enqueue_outbound(msg, state, deps)
    elif decision.action == RouteAction.HUMAN_REVIEW:
        await _create_review(msg, state, decision, intent, deps)

    # 10. Finalise the trace.
    state = state.replace(
        trace=state.trace.model_copy(
            update={
                "intent": intent,
                "intent_confidence": intent_confidence,
                "retrieval": retrieval,
                "draft": draft,
                "validators": list(validators),
                "confidence": breakdown,
                "decision": decision,
                "llm_calls": state.trace.llm_calls,
            }
        )
    )
    await deps.trace_repo.update(state.trace)
    await deps.trace_repo.finish(tid, decision)

    return decision


async def _enqueue_outbound(msg: InboundMessage, state: PipelineState, deps: Deps) -> None:
    """Put an auto-send draft in the outbox. The worker owns actual delivery."""
    if deps.outbox_repo is None or state.draft is None:
        logger.warning("auto_send_not_enqueued", reason="no outbox repository configured")
        return

    outbound = OutboundMessage(
        outbound_id=f"out-{uuid.uuid4().hex[:12]}",
        conversation_id=state.trace.conversation_id,
        tenant_id=msg.tenant_id,
        channel=msg.channel,
        recipient=msg.sender,
        subject=f"Re: {msg.subject}" if msg.subject else "Re: TaskFlow Support Request",
        body_text=state.draft.response_text,
        idempotency_key=OutboundMessage.make_idempotency_key(
            state.trace.conversation_id, state.draft.response_text
        ),
    )
    enqueued = await deps.outbox_repo.enqueue(outbound)
    if not enqueued:
        logger.info(
            "outbound_already_enqueued",
            idempotency_key=outbound.idempotency_key,
            conversation_id=outbound.conversation_id,
        )


async def _create_review(
    msg: InboundMessage,
    state: PipelineState,
    decision: RoutingDecision,
    intent: Any,
    deps: Deps,
) -> None:
    """Create the review item and alert a human. SLA comes from config, not from code."""
    if deps.review_repo is None:
        logger.warning("human_review_not_persisted", reason="no review repository configured")
        return

    now = datetime.now(UTC)
    item = ReviewItem(
        review_id=f"rev-{uuid.uuid4().hex[:12]}",
        trace_id=state.trace.trace_id,
        conversation_id=state.trace.conversation_id,
        tenant_id=msg.tenant_id,
        state=ReviewState.PENDING,
        draft=state.draft,
        decision=decision,
        created_at=now,
        sla_deadline=now + timedelta(minutes=sla_minutes_for(intent.value)),
        sender_email=msg.sender,
        # Redacted: this body is forwarded to Telegram/webhook by the alerter.
        inbound_body=msg.body_redacted,
    )
    await deps.review_repo.create(item)

    # An alert that fails must be visible. It does not fail the pipeline — the review
    # item is already durable — but it is recorded on the trace as an error.
    try:
        results = await dispatch_alert(item)
    except Exception as err:
        logger.error("alert_dispatch_raised", review_id=item.review_id, error=str(err))
        state.trace.errors.append(f"alert_dispatch_raised: {err}")
        return

    if not any(results.values()):
        logger.warning("alert_dispatch_no_channel_succeeded", review_id=item.review_id)
        state.trace.errors.append("alert_dispatch: no channel delivered")
    await deps.trace_repo.event(
        trace_id=state.trace.trace_id,
        stage="alert",
        payload={"review_id": item.review_id, "channels": results},
        elapsed_ms=0,
    )
