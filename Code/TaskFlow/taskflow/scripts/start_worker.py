#!/usr/bin/env python3
"""Outbox Delivery Worker Daemon CLI.

Polls the transactional outbox repository for QUEUED outbound replies,
delivers them via Gmail SMTP / Webhooks, and updates status to SENT.

Usage:
    uv run python scripts/start_worker.py
"""

import asyncio
import sys

import structlog

from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.repositories import SQLiteOutboxRepository
from taskflow.worker.outbox_worker import run_worker_loop

logger = structlog.get_logger()


async def main() -> int:
    print("================================================================================")
    print("      TASKFLOW OUTBOX DELIVERY WORKER DAEMON — ACTIVE")
    print("================================================================================")
    print("Polling database outbox for queued outbound replies every 1.0s...")

    db_url = "sqlite+aiosqlite:///./data/taskflow.db"
    engine = build_engine(db_url)
    factory = build_session_factory(engine)
    outbox_repo = SQLiteOutboxRepository(factory)

    try:
        await run_worker_loop(outbox_repo, poll_interval_s=1.0)
    except KeyboardInterrupt:
        print("\nWorker daemon stopped by user.")
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
