"""The state object passed down the pipeline.

Frozen. Stages advance it by producing a new copy via `replace()`, which removes an
entire class of "who mutated my state" bug.
"""

from dataclasses import dataclass, replace
from typing import Any

from taskflow.domain.enums import Intent
from taskflow.domain.models import (
    ConfidenceBreakdown,
    DraftOutput,
    InboundMessage,
    RetrievalResult,
    RoutingDecision,
    Trace,
    ValidatorResult,
)


@dataclass(frozen=True)
class PipelineState:
    message: InboundMessage
    trace: Trace
    intent: Intent | None = None
    intent_confidence: float = 0.0
    retrieval: RetrievalResult | None = None
    draft: DraftOutput | None = None
    validators: tuple[ValidatorResult, ...] = ()
    confidence: ConfidenceBreakdown | None = None
    decision: RoutingDecision | None = None

    def replace(self, **kwargs: Any) -> "PipelineState":
        """Return a copy with the given fields changed."""
        return replace(self, **kwargs)
