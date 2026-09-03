"""FastAPI application.

Transport only: parse, authenticate, delegate to a service, serialise. No business
logic lives here.
"""

from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from taskflow.adapters.db.repositories import (
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.bootstrap import runtime
from taskflow.config.settings import settings
from taskflow.domain.enums import Channel
from taskflow.domain.errors import ConfigError
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import Deps, run_pipeline
from taskflow.services.review.service import (
    approve_review,
    edit_and_approve_review,
    list_pending_reviews,
    reject_review,
)

logger = structlog.get_logger()


@dataclass
class AppState:
    """Everything built once at startup and shared by every request."""

    deps: Deps
    engine: Any
    trace_repo: SQLiteTraceRepository
    review_repo: SQLiteReviewRepository
    outbox_repo: SQLiteOutboxRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the application once, through the composition root.

    The API used to assemble its own repositories, router and vector store. Two
    wirings of the same system is one too many: the copy here was already a
    reranker behind `bootstrap.build_runtime`.
    """
    cfg = settings()

    async with runtime() as rt:
        app.state.app_state = AppState(
            deps=rt.deps,
            engine=rt.engine,
            trace_repo=rt.trace_repo,
            review_repo=rt.review_repo,
            outbox_repo=rt.outbox_repo,
        )
        logger.info("api_started", env=cfg.taskflow_env, tenant=cfg.taskflow_tenant_id)
        yield


app = FastAPI(title="TaskFlow Customer Support AI API", version="1.0.0", lifespan=lifespan)

_bearer = HTTPBearer(auto_error=False)


def _state(request: Request) -> AppState:
    return request.app.state.app_state  # type: ignore[no-any-return]


async def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Guard every endpoint that ingests a message or actions a review.

    TASKFLOW_API_TOKEN was a configured-but-unenforced setting. It is enforced here.
    An unset token is a startup-time misconfiguration, not an open door.
    """
    expected = settings().taskflow_api_token
    if not expected or expected == "change-me":
        raise ConfigError("TASKFLOW_API_TOKEN is not set; refusing to serve a protected endpoint")
    if credentials is None or credentials.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


Protected = Depends(require_token)


class InboundMessageRequest(BaseModel):
    message_id: str = Field(min_length=1)
    tenant_id: str = "taskflow-demo"
    channel: Channel = Channel.CONSOLE
    sender: str = Field(min_length=1)
    subject: str | None = None
    body_text: str = Field(min_length=1)


class OperatorRequest(BaseModel):
    operator: str = Field(default="operator_admin", min_length=1)
    reason: str | None = None


class EditReviewRequest(BaseModel):
    edited_text: str = Field(min_length=1)
    operator: str = Field(default="operator_admin", min_length=1)
    reason: str = "human edit"


@app.get("/", summary="API root")
async def root() -> dict[str, Any]:
    return {
        "service": "TaskFlow Customer Support AI API",
        "status": "online",
        "docs_url": "/docs",
        "authentication": "Bearer TASKFLOW_API_TOKEN on all endpoints except / and /healthz",
        "available_endpoints": {
            "health_check": "GET /healthz",
            "ingest_message": "POST /messages",
            "list_traces": "GET /traces",
            "get_trace": "GET /traces/{trace_id}",
            "list_reviews": "GET /reviews",
            "metrics": "GET /metrics",
        },
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "taskflow-api"}


@app.post("/messages", dependencies=[Protected])
async def ingest_message(req: InboundMessageRequest, request: Request) -> dict[str, Any]:
    deps = _state(request).deps
    msg = InboundMessage(
        message_id=req.message_id,
        dedupe_key=InboundMessage.make_dedupe_key(req.channel, req.message_id),
        tenant_id=req.tenant_id,
        channel=req.channel,
        sender=req.sender,
        subject=req.subject,
        body_text=req.body_text,
        # Overwritten by the pipeline's redaction stage; never trusted from a client.
        body_redacted=req.body_text,
        provider_message_id=req.message_id,
        received_at=datetime.now(UTC),
    )

    decision = await run_pipeline(msg, deps)
    return {
        "message_id": req.message_id,
        "action": decision.action.value,
        "reason": decision.reason,
        "reason_code": decision.reason_code,
        "score": decision.confidence.score if decision.confidence else None,
        "threshold": decision.confidence.threshold if decision.confidence else None,
    }


@app.get("/traces", dependencies=[Protected])
async def list_traces(
    request: Request, limit: int = Query(default=50, ge=1, le=200)
) -> list[dict[str, Any]]:
    traces = await _state(request).trace_repo.recent(limit=limit)
    return [t.model_dump(mode="json") for t in traces]


@app.get("/traces/{trace_id}", dependencies=[Protected])
async def get_trace(trace_id: str, request: Request) -> dict[str, Any]:
    trace = await _state(request).trace_repo.get(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace.model_dump(mode="json")


@app.get("/reviews", dependencies=[Protected])
async def list_reviews(request: Request) -> list[dict[str, Any]]:
    pending = await list_pending_reviews(_state(request).review_repo)
    return [item.model_dump(mode="json") for item in pending]


@app.post("/reviews/{review_id}/approve", dependencies=[Protected])
async def approve_review_endpoint(
    review_id: str, req: OperatorRequest, request: Request
) -> dict[str, str]:
    st = _state(request)
    ok = await approve_review(st.review_repo, st.outbox_repo, review_id, operator=req.operator)
    if not ok:
        raise HTTPException(status_code=409, detail="Review not found or no longer pending")
    return {"status": "approved", "review_id": review_id}


@app.post("/reviews/{review_id}/edit", dependencies=[Protected])
async def edit_review_endpoint(
    review_id: str, req: EditReviewRequest, request: Request
) -> dict[str, str]:
    st = _state(request)
    ok = await edit_and_approve_review(
        st.review_repo,
        st.outbox_repo,
        review_id,
        edited_text=req.edited_text,
        operator=req.operator,
        reason=req.reason,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Review not found or no longer pending")
    return {"status": "edited_and_approved", "review_id": review_id}


@app.post("/reviews/{review_id}/reject", dependencies=[Protected])
async def reject_review_endpoint(
    review_id: str, req: OperatorRequest, request: Request
) -> dict[str, str]:
    ok = await reject_review(
        _state(request).review_repo, review_id, operator=req.operator, reason=req.reason or ""
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Review not found or no longer pending")
    return {"status": "rejected", "review_id": review_id}


@app.get("/metrics", dependencies=[Protected])
async def get_metrics(request: Request) -> dict[str, Any]:
    """Aggregates over recent traces. Every figure below is derived, none is asserted."""
    traces = await _state(request).trace_repo.recent(limit=200)

    actions: Counter[str] = Counter()
    intents: Counter[str] = Counter()
    reason_codes: Counter[str] = Counter()
    total_cost = 0.0
    failovers = 0

    for t in traces:
        if t.decision:
            actions[t.decision.action.value] += 1
            reason_codes[t.decision.reason_code] += 1
        if t.intent:
            intents[t.intent.value] += 1
        for call in t.llm_calls:
            total_cost += call.cost_usd
            if call.failed_over:
                failovers += 1

    total = len(traces)
    decided = sum(actions.values())

    def pct(n: int) -> float:
        return round(n / decided * 100.0, 2) if decided else 0.0

    return {
        "total_traces": total,
        "decided_traces": decided,
        "actions_breakdown": dict(actions),
        "intents_breakdown": dict(intents),
        "reason_codes": dict(reason_codes),
        "auto_send_rate_pct": pct(actions.get("auto_send", 0)),
        "human_review_rate_pct": pct(actions.get("human_review", 0)),
        "duplicate_dropped": actions.get("dropped_duplicate", 0),
        "provider_failovers": failovers,
        "total_cost_usd": round(total_cost, 6),
    }
