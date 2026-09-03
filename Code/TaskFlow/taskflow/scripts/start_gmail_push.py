#!/usr/bin/env python3
"""Gmail Push listener: Watch -> Pub/Sub pull -> POST /messages.

Needs the one-time setup in docs/14_GMAIL_PUSH_SETUP.md (a GCP project, a Pub/Sub
topic+subscription granted to Gmail's push service account, and an OAuth consent
run via `scripts/gmail_watch_setup.py`) before this will receive anything.

Like the IMAP poller, this process never opens the vector store — it posts every
message to the running API, which is why the API must already be up.

    uv run python scripts/start_gmail_push.py
"""

import asyncio
import sys

from taskflow.adapters.channels.gmail_push import GmailPushListener
from taskflow.config.settings import settings
from taskflow.domain.errors import ConfigError


async def main() -> int:
    cfg = settings()
    if not cfg.gmail_user:
        print("GMAIL_USER must be set in .env.", file=sys.stderr)
        return 2
    if not cfg.taskflow_api_token or cfg.taskflow_api_token == "change-me":
        print("TASKFLOW_API_TOKEN must be set in .env to post to the API.", file=sys.stderr)
        return 2
    if cfg.gmail_inbound_mode != "push":
        print(
            f"Note: GMAIL_INBOUND_MODE={cfg.gmail_inbound_mode!r} in .env, but this is the push "
            "listener — set it to 'push', or ignore this if you're testing push deliberately.",
            file=sys.stderr,
        )

    try:
        listener = GmailPushListener()
    except ConfigError as err:
        print(str(err), file=sys.stderr)
        return 2

    print(
        f"TaskFlow Gmail push listener — watching {cfg.gmail_user}, posting to "
        f"{listener.api_base_url}. Ctrl-C to stop."
    )
    try:
        await listener.run_listener_loop()
    except ConfigError as err:
        print(str(err), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nListener stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
