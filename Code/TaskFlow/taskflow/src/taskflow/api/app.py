"""FastAPI REST API Server for TaskFlow (Phase P9).

Provides endpoints for message ingestion, trace & event log inspection, human review actions,
and real-time operational metrics.
"""

from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.orm import Base
from taskflow.adapters.db.repositories import (
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.adapters.llm.claude import ClaudeProvider
from taskflow.adapters.llm.ollama import OllamaProvider
from taskflow.adapters.llm.openrouter import OpenRouterProvider
from taskflow.adapters.llm.router import ProviderRouter
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import Deps, run_pipeline
from taskflow.services.review.service import (
    approve_review,
    edit_and_approve_review,
    list_pending_reviews,
    reject_review,
)

# Global app dependencies container
deps_container: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from taskflow.config.settings import settings
    db_url = settings().database_url
    engine = build_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = build_session_factory(engine)
    trace_repo = SQLiteTraceRepository(factory)
    review_repo = SQLiteReviewRepository(factory)
    outbox_repo = SQLiteOutboxRepository(factory)

    # Load providers from configuration
    from taskflow.adapters.llm.router import ProviderRouter
    llm_router = ProviderRouter()
    vector_store = QdrantVectorStore()

    deps_container["deps"] = Deps(
        trace_repo=trace_repo,
        llm_router=llm_router,
        vector_store=vector_store,
        review_repo=review_repo,
        outbox_repo=outbox_repo,
    )
    deps_container["engine"] = engine
    deps_container["review_repo"] = review_repo
    deps_container["outbox_repo"] = outbox_repo
    deps_container["trace_repo"] = trace_repo

    yield

    await engine.dispose()


app = FastAPI(title="TaskFlow Customer Support AI API", version="1.0.0", lifespan=lifespan)


class InboundMessageRequest(BaseModel):
    message_id: str
    tenant_id: str = "taskflow-demo"
    channel: Channel = Channel.CONSOLE
    sender: str
    subject: str | None = None
    body_text: str


class OperatorRequest(BaseModel):
    operator: str = "operator_admin"
    reason: str | None = None


class EditReviewRequest(BaseModel):
    edited_text: str
    operator: str = "operator_admin"
    reason: str = "human edit"


from fastapi.responses import Response


@app.get("/", summary="API Root Endpoint")
async def root():
    return {
        "service": "TaskFlow Customer Support AI API",
        "status": "online",
        "docs_url": "/docs",
        "available_endpoints": {
            "health_check": "GET /healthz",
            "ingest_message": "POST /messages",
            "list_traces": "GET /traces",
            "list_reviews": "GET /reviews",
            "metrics": "GET /metrics",
            "swagger_docs": "GET /docs",
        },
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "taskflow-api"}


@app.post("/messages")
async def ingest_message(req: InboundMessageRequest):
    deps: Deps = deps_container["deps"]
    msg = InboundMessage(
        message_id=req.message_id,
        dedupe_key=f"{req.tenant_id}:{req.message_id}",
        tenant_id=req.tenant_id,
        channel=req.channel,
        sender=req.sender,
        subject=req.subject,
        body_text=req.body_text,
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
        "score": decision.confidence.score if decision.confidence else 0.0,
    }


@app.get("/traces")
async def list_traces(limit: int = Query(default=50, ge=1, le=200)):
    trace_repo: SQLiteTraceRepository = deps_container["trace_repo"]
    traces = await trace_repo.recent(limit=limit)
    return [t.model_dump(mode="json") for t in traces]


@app.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    trace_repo: SQLiteTraceRepository = deps_container["trace_repo"]
    trace = await trace_repo.get(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace.model_dump(mode="json")


@app.get("/reviews")
async def list_reviews():
    review_repo: SQLiteReviewRepository = deps_container["review_repo"]
    pending = await list_pending_reviews(review_repo)
    return [item.model_dump(mode="json") for item in pending]


@app.post("/reviews/{review_id}/approve")
async def approve_review_endpoint(review_id: str, req: OperatorRequest):
    review_repo = deps_container["review_repo"]
    outbox_repo = deps_container["outbox_repo"]
    success = await approve_review(
        review_repo, outbox_repo, review_id=review_id, operator=req.operator
    )
    if not success:
        raise HTTPException(status_code=400, detail="Review item not found or not pending")
    return {"status": "approved", "review_id": review_id}


@app.post("/reviews/{review_id}/edit")
async def edit_review_endpoint(review_id: str, req: EditReviewRequest):
    review_repo = deps_container["review_repo"]
    outbox_repo = deps_container["outbox_repo"]
    success = await edit_and_approve_review(
        review_repo,
        outbox_repo,
        review_id=review_id,
        edited_text=req.edited_text,
        operator=req.operator,
        reason=req.reason,
    )
    if not success:
        raise HTTPException(status_code=400, detail="Review item not found or not pending")
    return {"status": "edited_and_approved", "review_id": review_id}


@app.post("/reviews/{review_id}/reject")
async def reject_review_endpoint(review_id: str, req: OperatorRequest):
    review_repo = deps_container["review_repo"]
    success = await reject_review(
        review_repo, review_id=review_id, operator=req.operator, reason=req.reason or ""
    )
    if not success:
        raise HTTPException(status_code=400, detail="Review item not found or not pending")
    return {"status": "rejected", "review_id": review_id}


@app.get("/metrics")
async def get_metrics():
    trace_repo: SQLiteTraceRepository = deps_container["trace_repo"]
    traces = await trace_repo.recent(limit=200)

    total_traces = len(traces)
    actions: Counter[str] = Counter()
    total_cost_usd = 0.0

    for t in traces:
        if t.decision:
            actions[t.decision.action.value] += 1
        for call in t.llm_calls:
            total_cost_usd += call.cost_usd

    auto_send_count = actions.get("auto_send", 0)
    human_review_count = actions.get("human_review", 0)

    auto_send_rate = (auto_send_count / total_traces * 100.0) if total_traces > 0 else 0.0
    human_review_rate = (human_review_count / total_traces * 100.0) if total_traces > 0 else 0.0

    return {
        "total_traces": total_traces,
        "actions_breakdown": dict(actions),
        "auto_send_rate_pct": round(auto_send_rate, 2),
        "human_review_rate_pct": round(human_review_rate, 2),
        "total_cost_usd": round(total_cost_usd, 6),
    }
