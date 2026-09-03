#!/usr/bin/env python3
"""One-time OAuth consent for Gmail Push. Run this once before `make poller-push`.

Opens a browser for Google's consent screen, then writes the resulting token to
`GMAIL_TOKEN_PATH` (default `secrets/token.json`). Requires `GMAIL_CREDENTIALS_PATH`
(default `secrets/credentials.json`) — a Desktop-app OAuth client downloaded from
Google Cloud Console. See docs/14_GMAIL_PUSH_SETUP.md for every step before this one.

After consent, this also registers the Gmail watch once and confirms it — the
fastest way to find out the Pub/Sub publish-permission grant was missed, rather
than discovering it later when the listener silently receives nothing.

    uv run python scripts/gmail_watch_setup.py
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from taskflow.adapters.channels.gmail_push import GMAIL_SCOPES, GmailPushListener
from taskflow.config.settings import settings


def main() -> int:
    cfg = settings()
    creds_path = Path(cfg.gmail_credentials_path)
    token_path = Path(cfg.gmail_token_path)

    if not creds_path.exists():
        print(
            f"{creds_path} not found. Download a Desktop-app OAuth client from Google Cloud "
            "Console (APIs & Services -> Credentials) and save it there — "
            "see docs/14_GMAIL_PUSH_SETUP.md step 2.",
            file=sys.stderr,
        )
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"Wrote {token_path}.")

    if not cfg.gcp_project_id or not cfg.pubsub_topic or not cfg.pubsub_subscription:
        print(
            "GCP_PROJECT_ID / PUBSUB_TOPIC / PUBSUB_SUBSCRIPTION not set in .env yet — "
            "skipping the watch registration check. Set them, then re-run this script.",
        )
        return 0

    print("Registering the Gmail watch to confirm the Pub/Sub topic permission...")
    listener = GmailPushListener()
    history_id = listener.register_watch()
    print(f"Watch registered. Baseline historyId={history_id}. Ready for `make poller-push`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
