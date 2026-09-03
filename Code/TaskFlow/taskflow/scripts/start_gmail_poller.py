#!/usr/bin/env python3
"""Gmail inbound poller.

Polls Gmail over IMAP for unseen mail and posts each message to the running API's
`POST /messages` endpoint. Deduplication is enforced there against the RFC-822
Message-ID, so a redelivered mail is dropped rather than answered twice.

This process never opens the vector store — only the API does — so it can run
alongside `make api` without the two of them fighting over the embedded Qdrant
store's exclusive file lock. Start the API first: this will fail every poll until
it's reachable.

    uv run python scripts/start_gmail_poller.py
"""

import asyncio
import sys

from taskflow.adapters.channels.gmail_poller import GmailIMAPPoller
from taskflow.config.settings import settings


async def main() -> int:
    cfg = settings()
    if not cfg.gmail_user or not cfg.gmail_app_password:
        print(
            "GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env to poll Gmail.",
            file=sys.stderr,
        )
        return 2
    if not cfg.taskflow_api_token or cfg.taskflow_api_token == "change-me":
        print("TASKFLOW_API_TOKEN must be set in .env to post to the API.", file=sys.stderr)
        return 2
    if cfg.gmail_inbound_mode == "push":
        print(
            "Note: GMAIL_INBOUND_MODE=push in .env, but this is the IMAP poller — "
            "run `make poller-push` instead, or ignore this if you're testing IMAP deliberately.",
            file=sys.stderr,
        )

    poller = GmailIMAPPoller()
    print(
        f"TaskFlow Gmail poller — watching {cfg.gmail_user}, posting to "
        f"{poller.api_base_url} every {poller.poll_interval_s:.0f}s. Ctrl-C to stop."
    )
    try:
        await poller.run_poller_loop()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nPoller stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
