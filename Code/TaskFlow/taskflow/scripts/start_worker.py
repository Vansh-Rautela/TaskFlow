#!/usr/bin/env python3
"""Outbox delivery worker.

Claims QUEUED outbound replies and delivers them over Gmail SMTP. A message is
marked SENT only when SMTP accepted it; anything else is retried and then marked
DEAD once `worker.max_attempts` is reached.

    uv run python scripts/start_worker.py
"""

import asyncio
import sys

import structlog

from taskflow.bootstrap import runtime
from taskflow.worker.outbox_worker import run_worker_loop

logger = structlog.get_logger()


async def main() -> int:
    # The worker never retrieves, so it skips loading the embedding models.
    async with runtime(with_vector_store=False) as rt:
        if not await rt.email_channel.health():
            print(
                "SMTP is not configured (GMAIL_USER / GMAIL_APP_PASSWORD are unset).\n"
                "The worker would mark every message DEAD. Configure .env and retry.",
                file=sys.stderr,
            )
            return 2

        print("TaskFlow outbox delivery worker — running. Ctrl-C to stop.")
        try:
            await run_worker_loop(rt.outbox_repo, rt.email_channel)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nWorker stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
