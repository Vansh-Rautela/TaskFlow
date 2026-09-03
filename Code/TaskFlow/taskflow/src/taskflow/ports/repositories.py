from datetime import datetime
from typing import Any, Protocol

from taskflow.domain.enums import OutboxState, ReviewState
from taskflow.domain.models import (
    EditRecord,
    InboundMessage,
    OutboundMessage,
    ReviewItem,
    RoutingDecision,
    Trace,
)


class TraceRepository(Protocol):
    async def start(self, msg: InboundMessage) -> Trace: ...
    async def event(self, trace_id: str, stage: str, payload: dict, elapsed_ms: int) -> None: ...
    async def update(self, trace: Trace) -> None: ...
    async def finish(self, trace_id: str, decision: RoutingDecision) -> None: ...
    async def get(self, trace_id: str) -> Trace | None: ...
    async def recent(self, limit: int = 50) -> list[Trace]: ...


class ReviewRepository(Protocol):
    async def create(self, item: ReviewItem) -> None: ...
    async def get(self, review_id: str) -> ReviewItem | None: ...
    async def pending(self) -> list[ReviewItem]: ...
    async def due(self, now: datetime) -> list[ReviewItem]: ...
    async def all(self, limit: int = 500) -> list[ReviewItem]:
        """Every review regardless of state, most recent first — the operator queue

        only needs `pending()`; this is for offline analysis (`scripts/calibrate_confidence.py`)
        that needs the actioned ones too.
        """
        ...

    async def transition(self, review_id: str, to: ReviewState, expected: ReviewState) -> bool:
        """Optimistic lock: UPDATE ... WHERE state = :expected. False if someone won the race."""
        ...

    async def record_edit(self, edit: EditRecord) -> None: ...
    async def edits_for(self, review_id: str) -> list[EditRecord]: ...


class OutboxRepository(Protocol):
    async def enqueue(self, msg: OutboundMessage) -> bool:
        """False when idempotency_key already exists — the first line of send-once defence."""
        ...

    async def claim(self, limit: int = 10) -> list[OutboundMessage]:
        """Atomically move QUEUED -> SENDING and stamp a claim time for the lease."""
        ...

    async def mark(
        self,
        outbound_id: str,
        state: OutboxState,
        error: str | None = None,
        increment_retry: bool = False,
    ) -> None:
        """Record the terminal (or retryable) outcome of a delivery attempt.

        FAILED returns the row to QUEUED for another attempt; DEAD and SENT are final.
        """
        ...

    async def requeue_expired(self, lease_minutes: int) -> int:
        """Return rows stuck in SENDING past the lease to QUEUED. Returns the count."""
        ...

    async def get(self, outbound_id: str) -> OutboundMessage | None: ...


class AuditRepository(Protocol):
    async def record(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None: ...

    async def for_entity(self, entity_id: str) -> list[dict[str, Any]]: ...


class ProcessedMessageRepository(Protocol):
    async def try_claim(self, dedupe_key: str) -> bool:
        """False when the key exists. Backed by a PRIMARY KEY, not SELECT-then-INSERT."""
        ...
