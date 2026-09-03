"""Test doubles.

Each one stands in for a real boundary — a provider, a vector store, a transport —
and implements the same Protocol the production adapter does. A double whose shape
has drifted from its port is worse than no double: it lets a test pass against an
interface that does not exist.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from taskflow.domain.enums import Channel
from taskflow.domain.errors import AllProvidersFailed, ProviderError
from taskflow.domain.models import (
    Chunk,
    DocumentChunk,
    LLMCall,
    OutboundMessage,
    RetrievalResult,
    ScoredChunk,
)
from taskflow.ports.channel import DeliveryReceipt


class FakeLLMRouter:
    """Satisfies ports.llm.LLMRouter. Canned structured responses per purpose."""

    name = "fake"

    def __init__(
        self,
        responses: dict[str, BaseModel] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._responses = responses or {}
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(
        self, *, purpose: str, system: str, user: str, schema: type[BaseModel]
    ) -> tuple[Any, LLMCall]:
        self.calls.append({"purpose": purpose, "system": system, "user": user})
        if self._raises:
            raise self._raises
        if purpose not in self._responses:
            raise AllProvidersFailed(f"no canned response configured for purpose {purpose!r}")
        return self._responses[purpose], LLMCall(
            purpose=purpose,
            provider=self.name,
            model="fake-model",
            prompt_tokens=100,
            completion_tokens=200,
            cost_usd=0.0,
            latency_ms=1,
        )

    def prompts_for(self, purpose: str) -> list[str]:
        """Every user prompt sent for a purpose — used to assert what reached the model."""
        return [c["user"] for c in self.calls if c["purpose"] == purpose]


class FakeVectorStore:
    """Satisfies ports.vector_store.VectorStore with a fixed result set."""

    def __init__(
        self, chunks: list[Chunk] | None = None, scores: list[float] | None = None
    ) -> None:
        self._chunks = chunks or []
        self._scores = scores if scores is not None else [0.9] * len(self._chunks)
        self.ingested: list[DocumentChunk] = []
        self.queries: list[str] = []
        self.last_limit: int | None = None

    def setup_collection(self, recreate: bool = False) -> None:
        self.recreated = recreate

    def ingest(self, chunks: list[DocumentChunk], tenant_id: str) -> None:
        self.ingested.extend(chunks)

    def search(self, query: str, tenant_id: str, limit: int = 5) -> RetrievalResult:
        self.queries.append(query)
        self.last_limit = limit
        scored = [
            ScoredChunk(chunk=c, rrf_score=s)
            for c, s in zip(self._chunks, self._scores, strict=False)
        ][:limit]
        return RetrievalResult(query_used=query, chunks=scored, sufficient=False, latency_ms=0)


class FakeReranker:
    """Satisfies ports.reranker.Reranker.

    Reorders by a caller-supplied score per chunk_id so a test can state the ordering
    it wants without running a model. Anything unlisted scores 0.0.
    """

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self._scores = scores or {}
        self.queries: list[str] = []

    def rerank(self, query: str, chunks: list[ScoredChunk], limit: int) -> list[ScoredChunk]:
        self.queries.append(query)
        scored = [
            sc.model_copy(update={"rerank_score": self._scores.get(sc.chunk.chunk_id, 0.0)})
            for sc in chunks
        ]
        scored.sort(key=lambda sc: sc.final_score, reverse=True)
        return scored[:limit]


class RecordingChannel:
    """Satisfies ports.channel.OutboundChannel. Records sends instead of making them."""

    channel: Channel = Channel.EMAIL

    def __init__(self, fail_with: Exception | None = None, fail_times: int | None = None) -> None:
        self._fail_with = fail_with
        self._fail_times = fail_times
        self.sent: list[OutboundMessage] = []
        self.attempts = 0

    async def health(self) -> bool:
        return True

    async def send(self, outbound: OutboundMessage) -> DeliveryReceipt:
        self.attempts += 1
        if self._fail_with is not None and (
            self._fail_times is None or self.attempts <= self._fail_times
        ):
            raise self._fail_with
        self.sent.append(outbound)
        return DeliveryReceipt(
            provider_message_id=f"fake-{outbound.outbound_id}", delivered_at=datetime.now(UTC)
        )


class FakeLLMProvider:
    """Satisfies ports.llm.LLMProvider — the per-provider level, below the router."""

    def __init__(
        self, name: str = "fake", raises: Exception | None = None, text: str = "{}"
    ) -> None:
        self.name = name
        self._raises = raises or None
        self._text = text
        self.calls = 0

    async def health(self) -> bool:
        return self._raises is None

    async def complete(self, **kwargs: Any) -> Any:
        from taskflow.ports.llm import LLMResponse

        self.calls += 1
        if self._raises:
            raise self._raises
        return LLMResponse(
            text=self._text,
            call=LLMCall(
                purpose="complete",
                provider=self.name,
                model=kwargs.get("model", "fake-model"),
                prompt_tokens=10,
                completion_tokens=20,
                cost_usd=0.0,
                latency_ms=1,
            ),
        )


def unreachable_provider(name: str = "down") -> FakeLLMProvider:
    return FakeLLMProvider(name=name, raises=ProviderError(f"{name} is unreachable"))


def chunk(chunk_id: str, text: str, title: str = "Doc", tenant_id: str = "test") -> Chunk:
    """A retrieved chunk with sensible defaults, to keep tests readable."""
    return Chunk(
        chunk_id=chunk_id,
        doc_id=chunk_id.split(":")[0],
        title=title,
        section=None,
        text=text,
        version="1.0",
        source_path="",
        tenant_id=tenant_id,
    )
