"""Composition root.

The one place adapters are chosen and wired to ports. Every entrypoint — API,
worker, poller, scripts — builds its dependencies here, so they cannot drift into
using different databases, skipping migrations, or omitting a repository.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from taskflow.adapters.channels.smtp_email import SMTPEmailChannel
from taskflow.adapters.db.bootstrap import run_migrations, run_migrations_async
from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.repositories import (
    SQLiteAuditRepository,
    SQLiteInboxRepository,
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.adapters.llm.router import ProviderRouter
from taskflow.adapters.pii.presidio_detector import PresidioDetector
from taskflow.adapters.rerank.cross_encoder import CrossEncoderReranker
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.config.settings import settings
from taskflow.pipeline.orchestrator import Deps

logger = structlog.get_logger()


@dataclass
class Runtime:
    """A fully wired application. Close it with `aclose()`."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    deps: Deps
    trace_repo: SQLiteTraceRepository
    review_repo: SQLiteReviewRepository
    outbox_repo: SQLiteOutboxRepository
    inbox_repo: SQLiteInboxRepository
    audit_repo: SQLiteAuditRepository
    email_channel: SMTPEmailChannel

    async def aclose(self) -> None:
        await self.engine.dispose()


def build_runtime(
    database_url: str | None = None,
    *,
    migrate: bool = True,
    with_vector_store: bool = True,
) -> Runtime:
    """Wire the application against `database_url` (defaults to settings).

    `with_vector_store=False` skips loading the embedding, reranker, and PII-detection
    models, which take several seconds — useful for entrypoints that never run the
    pipeline, such as the outbox worker and the SLA sweeper.

    `migrate=True` is only safe from a synchronous context: Alembic drives its async
    engine with `asyncio.run()`. Callers already inside a loop should await
    `adapters.db.bootstrap.run_migrations_async` first and pass `migrate=False`.
    """
    url = database_url or settings().database_url

    if migrate:
        run_migrations(url)

    engine = build_engine(url)
    factory = build_session_factory(engine)

    trace_repo = SQLiteTraceRepository(factory)
    review_repo = SQLiteReviewRepository(factory)
    outbox_repo = SQLiteOutboxRepository(factory)
    inbox_repo = SQLiteInboxRepository(factory)
    audit_repo = SQLiteAuditRepository(factory)

    deps = Deps(
        trace_repo=trace_repo,
        llm_router=ProviderRouter(),
        vector_store=QdrantVectorStore() if with_vector_store else None,  # type: ignore[arg-type]
        reranker=CrossEncoderReranker() if with_vector_store else None,
        review_repo=review_repo,
        outbox_repo=outbox_repo,
        processed_repo=inbox_repo,
        pii_detector=PresidioDetector() if with_vector_store else None,
    )

    return Runtime(
        engine=engine,
        session_factory=factory,
        deps=deps,
        trace_repo=trace_repo,
        review_repo=review_repo,
        outbox_repo=outbox_repo,
        inbox_repo=inbox_repo,
        audit_repo=audit_repo,
        email_channel=SMTPEmailChannel(),
    )


@asynccontextmanager
async def runtime(
    database_url: str | None = None,
    *,
    migrate: bool = True,
    with_vector_store: bool = True,
) -> AsyncIterator[Runtime]:
    """`build_runtime` for async callers, with the engine always disposed.

    Migrations run via `run_migrations_async`, which hands Alembic a worker thread:
    its env.py calls `asyncio.run()`, which raises inside an already-running loop.
    """
    if migrate:
        await run_migrations_async(database_url or settings().database_url)

    rt = build_runtime(database_url, migrate=False, with_vector_store=with_vector_store)
    try:
        yield rt
    finally:
        await rt.aclose()
