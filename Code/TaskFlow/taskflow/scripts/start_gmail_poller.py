#!/usr/bin/env python3
"""Live Gmail Inbound Email Poller Daemon CLI.

Polls imap.gmail.com:993 for unread emails sent to supportfortaskflow@gmail.com,
processes them through TaskFlow's 8-stage pipeline, redacts PII, checks policies,
dispatches Telegram/Email alerts on human review, and enqueues auto-send replies.

Usage:
    uv run python scripts/start_gmail_poller.py
"""

import asyncio
import sys

from taskflow.adapters.channels.gmail_poller import GmailIMAPPoller
from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.repositories import SQLiteTraceRepository
from taskflow.adapters.db.repositories import SQLiteReviewRepository, SQLiteOutboxRepository
from taskflow.adapters.llm.router import ProviderRouter
from taskflow.config.settings import settings
from taskflow.pipeline.orchestrator import Deps


async def main() -> int:
    print("================================================================================")
    print("      TASKFLOW GMAIL INBOUND EMAIL POLLER DAEMON — ACTIVE")
    print("================================================================================")
    print(f"📧 Listening to Gmail: {settings().gmail_user or 'supportfortaskflow@gmail.com'}")
    print(f"⏱️  Polling Interval: {settings().gmail_poll_seconds or 15} seconds")
    print("================================================================================")

    db_url = "sqlite+aiosqlite:///./data/taskflow.db"
    engine = build_engine(db_url)
    factory = build_session_factory(engine)

    trace_repo = SQLiteTraceRepository(factory)
    review_repo = SQLiteReviewRepository(factory)
    outbox_repo = SQLiteOutboxRepository(factory)

    llm_router = ProviderRouter()
    deps = Deps(
        trace_repo=trace_repo,
        llm_router=llm_router,
        review_repo=review_repo,
        outbox_repo=outbox_repo,
    )

    poller = GmailIMAPPoller(deps=deps, poll_interval_s=float(settings().gmail_poll_seconds or 15))

    try:
        await poller.run_poller_loop()
    except KeyboardInterrupt:
        print("\nGmail poller daemon stopped by user.")
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
