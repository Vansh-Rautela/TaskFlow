from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from taskflow.domain.enums import (
    Channel,
    GateId,
    Intent,
    OutboxState,
    ReviewState,
    RouteAction,
    Severity,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- classification ----------


class ClassificationOutput(Base):
    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


# ---------- ingestion ----------


class InboundMessage(Base):
    message_id: str  # our uuid
    dedupe_key: str  # "{channel}:{provider_message_id}"
    tenant_id: str
    channel: Channel
    sender: str
    subject: str | None = None
    body_text: str  # normalized, quoted replies stripped
    body_redacted: str  # PII-redacted; this is what reaches the LLM
    thread_ref: str | None = None  # gmail threadId | slack thread_ts
    provider_message_id: str
    provider_thread_headers: dict[str, str] = Field(default_factory=dict)  # In-Reply-To etc
    received_at: datetime
    raw_ref: str | None = None  # path/id of the archived original

    @staticmethod
    def make_dedupe_key(channel: Channel, provider_message_id: str) -> str:
        return f"{channel.value}:{provider_message_id}"


class ThreadContext(Base):
    conversation_id: str
    turns: list[dict[str, str]] = Field(default_factory=list)  # {role, text, at}
    prior_intents: list[Intent] = Field(default_factory=list)
    prior_escalations: int = 0


# ---------- retrieval ----------


class Chunk(Base):
    chunk_id: str  # "{doc_id}:{ordinal}"
    doc_id: str
    title: str
    section: str | None
    text: str
    doc_type: str
    product_tier: str | None = None
    intents: list[str] = Field(default_factory=list)
    version: str
    source_path: str
    tenant_id: str

    @staticmethod
    def make_doc_id(relpath: str, title: str) -> str:
        return hashlib.sha256(f"{relpath}\n{title}".encode()).hexdigest()[:16]


class ScoredChunk(Base):
    chunk: Chunk
    dense_score: float | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None

    @property
    def final_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else (self.rrf_score or 0.0)


class RetrievalResult(Base):
    query_used: str
    chunks: list[ScoredChunk]
    sufficient: bool
    gap_reason: str | None = None
    suspicious_context: bool = False  # instruction-like text found in a chunk
    latency_ms: int
    top_score: float = 0.0
    support_count: int = 0


# ---------- generation ----------


class Citation(Base):
    chunk_id: str
    doc_title: str
    section: str | None = None
    quote_span: str | None = None


class DraftOutput(Base):
    """The contract the drafting model must satisfy. Validated, never trusted."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    response_text: str = Field(
        validation_alias=AliasChoices("response_text", "response", "reply", "body"),
        min_length=1,
        max_length=4000,
    )
    citations: list[Citation] = Field(default_factory=list)
    tone: str = Field(default="neutral", pattern="^(formal|friendly|apologetic|neutral|technical)$")
    complexity: str = Field(default="simple", pattern="^(simple|moderate|complex)$")
    draft_confidence: float = Field(default=0.85, ge=0.0, le=1.0)  # LOGGED ONLY — never routed on

    @field_validator("citations", mode="before")
    @classmethod
    def _coerce_citations(cls, v: Any) -> list[Any]:
        if isinstance(v, list):
            res = []
            for item in v:
                if isinstance(item, str):
                    res.append({"chunk_id": item, "doc_title": item})
                else:
                    res.append(item)
            return res
        return v

    @model_validator(mode="after")
    def _citation_markers_present(self) -> Self:
        if self.citations:
            missing = [
                c.chunk_id for c in self.citations if f"[{c.chunk_id}]" not in self.response_text
            ]
            if missing:
                # If local model generated citations not inline, don't crash — strip unreferenced citations
                valid_citations = [c for c in self.citations if f"[{c.chunk_id}]" in self.response_text]
                object.__setattr__(self, "citations", valid_citations)
        return self


# ---------- validation ----------


class ValidatorResult(Base):
    validator_name: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    blocking: bool = False
    latency_ms: int = 0
    errored: bool = False


class PolicyViolation(Base):
    rule_id: str
    severity: Severity
    description: str
    matched_text: str


class GateResult(Base):
    gate_id: GateId
    passed: bool
    reason: str


class ConfidenceBreakdown(Base):
    """Two layers. Gates veto; the score only decides among survivors."""

    gates: list[GateResult]
    citation_coverage: float = 0.0
    grounding_entailment: float = 0.0
    retrieval_relevance: float = 0.0
    intent_confidence: float = 0.0
    tone_alignment: float = 0.0
    weights: dict[str, float]
    score: float = 0.0
    threshold: float = 1.0
    draft_confidence_logged: float = 0.0  # never an input to the decision

    @property
    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    @property
    def gates_passed(self) -> bool:
        return not self.failed_gates


class RoutingDecision(Base):
    action: RouteAction
    reason: str
    reason_code: str
    confidence: ConfidenceBreakdown | None = None
    decided_at: datetime = Field(default_factory=utcnow)


# ---------- human loop ----------


class ReviewItem(Base):
    review_id: str
    trace_id: str
    conversation_id: str
    tenant_id: str
    state: ReviewState
    draft: DraftOutput | None
    decision: RoutingDecision
    created_at: datetime
    sla_deadline: datetime
    escalated_at: datetime | None = None
    sender_email: str | None = None  # original customer sender for reply addressing
    inbound_body: str | None = None


class EditRecord(Base):
    review_id: str
    original_draft: str
    edited_draft: str
    editor: str
    edited_at: datetime
    reason: str | None = None
    char_diff: int


class OutboundMessage(Base):
    outbound_id: str
    conversation_id: str
    tenant_id: str
    channel: Channel
    recipient: str
    subject: str | None
    body_text: str
    reply_headers: dict[str, str] = Field(default_factory=dict)
    state: OutboxState = OutboxState.QUEUED
    idempotency_key: str
    retry_count: int = 0

    @staticmethod
    def make_idempotency_key(conversation_id: str, body_text: str) -> str:
        digest = hashlib.sha256(body_text.encode()).hexdigest()[:16]
        return f"{conversation_id}:{digest}"


# ---------- observability ----------


class LLMCall(Base):
    purpose: str  # draft | grounding | rubric | datagen
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    attempts: int = 1
    repaired: bool = False
    failed_over: bool = False


class TraceEvent(Base):
    trace_id: str
    stage: str
    payload: dict[str, Any]
    elapsed_ms: int
    at: datetime = Field(default_factory=utcnow)


class Trace(Base):
    trace_id: str
    conversation_id: str
    message_id: str
    tenant_id: str
    source_channel: Channel
    started_at: datetime
    finished_at: datetime | None = None
    intent: Intent | None = None
    intent_confidence: float | None = None
    classifier_version: str | None = None
    retrieval: RetrievalResult | None = None
    draft: DraftOutput | None = None
    validators: list[ValidatorResult] = Field(default_factory=list)
    confidence: ConfidenceBreakdown | None = None
    decision: RoutingDecision | None = None
    llm_calls: list[LLMCall] = Field(default_factory=list)
    delivery_result: str | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.llm_calls)
