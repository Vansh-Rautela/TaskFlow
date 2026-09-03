"""Gmail Push Adapter for TaskFlow (Watch -> Pub/Sub pull -> POST /messages).

Additive to `gmail_poller.py`, not a replacement — selected by
`GMAIL_INBOUND_MODE=push` once the one-time GCP setup in
`docs/14_GMAIL_PUSH_SETUP.md` is done. Deliberately a Pub/Sub **pull** subscriber,
not an HTTP push endpoint: nothing needs to be publicly reachable, matching this
project's "no servers" posture — the same way IMAP polling only ever makes outbound
connections.

Like the poller, this never opens the vector store: every message is posted to the
already-running API's `/messages` endpoint via the shared `_api_client`, so only the
API process ever holds the embedded Qdrant store's lock. See `gmail_poller.py`'s
module docstring for why that matters.

Gmail push notifications carry only `{emailAddress, historyId}` — a hint that
*something* changed, not what. On every notification this re-derives the actual
diff from the last durable cursor via `users.history.list`, which is the pattern
Gmail's own docs recommend: it's idempotent regardless of Pub/Sub's at-least-once,
possibly-reordered delivery.
"""

import asyncio
import base64
import json
from email import message_from_bytes
from pathlib import Path
from typing import Any, cast

import structlog
from google.auth.transport.requests import Request
from google.cloud import pubsub_v1
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from taskflow.adapters.channels._api_client import post_inbound_message
from taskflow.adapters.channels._email_parsing import (
    clean_email_address,
    decode_str,
    extract_body,
    is_ignorable_sender,
    strip_quoted_reply,
)
from taskflow.config.settings import settings
from taskflow.domain.enums import Channel
from taskflow.domain.errors import ConfigError, ProviderError, TransientError

logger = structlog.get_logger()

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/pubsub",
]
# Watches expire after 7 days (Gmail API limit); renew well inside that window so a
# missed renewal never silently goes dark.
WATCH_RENEWAL_INTERVAL_S = 12 * 60 * 60


def load_credentials(token_path: str) -> Credentials:
    """Load the OAuth token written by `scripts/gmail_watch_setup.py`, refreshing
    it if it has expired. Raises `ConfigError` if setup hasn't been run — there is
    no browser to consent through from inside a long-running listener."""
    path = Path(token_path)
    if not path.exists():
        raise ConfigError(
            f"{token_path} not found — run `uv run python scripts/gmail_watch_setup.py` "
            "once first (see docs/14_GMAIL_PUSH_SETUP.md)"
        )
    creds = cast(Credentials, Credentials.from_authorized_user_file(str(path), GMAIL_SCOPES))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return creds


def gmail_message_to_inbound_fields(
    raw_message_bytes: bytes, *, user_email: str, ops_email: str = ""
) -> dict[str, str] | None:
    """Map one fetched Gmail message (`users.messages.get(format='raw')`, already
    base64-decoded to raw RFC-822 bytes) to `post_inbound_message` kwargs, or None
    if it should be ignored (self-sent alert, system sender, no Message-ID).

    Pure and synchronous on purpose: this is the part of the push path worth
    testing without a live Gmail/Pub-Sub connection, built from the same
    `_email_parsing` helpers `gmail_poller.py` uses on its IMAP fetch — the two
    channels agree on what a message means down to this function.
    """
    msg_obj = message_from_bytes(raw_message_bytes)
    subject = decode_str(msg_obj.get("Subject"))
    sender = clean_email_address(decode_str(msg_obj.get("From")))
    message_id_hdr = decode_str(msg_obj.get("Message-ID"))
    body = extract_body(msg_obj)

    if not message_id_hdr:
        return None
    if is_ignorable_sender(
        sender=sender, subject=subject, user_email=user_email, ops_email=ops_email
    ):
        return None

    body_text = strip_quoted_reply(body) or subject or "No body text provided"
    return {
        "message_id": message_id_hdr,
        "sender": sender,
        "subject": subject,
        "body_text": body_text,
    }


class GmailPushListener:
    """Registers a Gmail watch, pulls Pub/Sub notifications, and posts new mail."""

    def __init__(
        self,
        api_base_url: str | None = None,
        api_token: str | None = None,
        state_path: str | None = None,
    ) -> None:
        cfg = settings()
        self.api_base_url = api_base_url or cfg.taskflow_api_base_url
        self.api_token = api_token or cfg.taskflow_api_token
        self.user = cfg.gmail_user
        self.ops_email = cfg.ops_email or ""
        self.project_id = cfg.gcp_project_id
        self.topic = cfg.pubsub_topic
        self.subscription = cfg.pubsub_subscription
        self.state_path = Path(state_path or "data/gmail_push_state.json")
        self.credentials = load_credentials(cfg.gmail_token_path)
        self.gmail: Any = build("gmail", "v1", credentials=self.credentials, cache_discovery=False)

    def _topic_path(self) -> str:
        return f"projects/{self.project_id}/topics/{self.topic}"

    def _subscription_path(self) -> str:
        return f"projects/{self.project_id}/subscriptions/{self.subscription}"

    def _load_cursor(self) -> str | None:
        if not self.state_path.exists():
            return None
        cursor = json.loads(self.state_path.read_text()).get("last_history_id")
        return str(cursor) if cursor is not None else None

    def _save_cursor(self, history_id: str) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"last_history_id": history_id}))

    def register_watch(self) -> str:
        """Register (or renew) the push watch. Blocking — call via an executor."""
        resp = (
            self.gmail.users()
            .watch(userId="me", body={"topicName": self._topic_path(), "labelIds": ["INBOX"]})
            .execute()
        )
        history_id = str(resp["historyId"])
        logger.info(
            "gmail_watch_registered", history_id=history_id, expiration=resp.get("expiration")
        )
        # First-ever registration: establish a baseline so the first notification
        # doesn't replay the whole mailbox's history.
        if self._load_cursor() is None:
            self._save_cursor(history_id)
        return history_id

    async def _renew_watch_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(WATCH_RENEWAL_INTERVAL_S)
            try:
                await loop.run_in_executor(None, self.register_watch)
            except Exception as err:  # a renewal failure must not kill the listener
                logger.error("gmail_watch_renewal_failed", error=str(err))

    def _fetch_new_raw_messages(self) -> list[bytes]:
        """Blocking: `history.list` from the durable cursor, then `messages.get`
        each added message as raw RFC-822 bytes. Advances and persists the cursor."""
        cursor = self._load_cursor()
        if cursor is None:
            self.register_watch()
            return []

        message_ids: list[str] = []
        page_token: str | None = None
        latest_history_id = cursor
        while True:
            resp = (
                self.gmail.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=cursor,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    message_ids.append(added["message"]["id"])
            latest_history_id = resp.get("historyId", latest_history_id)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        raw_messages = []
        for msg_id in message_ids:
            resource = (
                self.gmail.users().messages().get(userId="me", id=msg_id, format="raw").execute()
            )
            raw_messages.append(base64.urlsafe_b64decode(resource["raw"]))

        self._save_cursor(str(latest_history_id))
        return raw_messages

    async def _handle_notification(self) -> int:
        loop = asyncio.get_running_loop()
        raw_messages = await loop.run_in_executor(None, self._fetch_new_raw_messages)

        processed = 0
        for raw in raw_messages:
            fields = gmail_message_to_inbound_fields(
                raw, user_email=self.user, ops_email=self.ops_email
            )
            if fields is None:
                continue
            try:
                result = await post_inbound_message(
                    api_base_url=self.api_base_url,
                    api_token=self.api_token,
                    message_id=fields["message_id"],
                    tenant_id=settings().taskflow_tenant_id or "taskflow-demo",
                    channel=Channel.EMAIL.value,
                    sender=fields["sender"],
                    subject=fields["subject"],
                    body_text=fields["body_text"],
                )
                processed += 1
                logger.info(
                    "gmail_push_processed_inbound",
                    action=result.get("action"),
                    message_id=fields["message_id"],
                )
            except (TransientError, ProviderError) as err:
                logger.error(
                    "gmail_push_api_post_failed", message_id=fields["message_id"], error=str(err)
                )
        return processed

    async def run_listener_loop(self) -> None:
        """Register the watch, then pull Pub/Sub notifications until cancelled."""
        if not self.project_id or not self.topic or not self.subscription:
            raise ConfigError(
                "GCP_PROJECT_ID, PUBSUB_TOPIC and PUBSUB_SUBSCRIPTION must be set "
                "(see docs/14_GMAIL_PUSH_SETUP.md)"
            )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.register_watch)

        subscriber = pubsub_v1.SubscriberClient(credentials=self.credentials)
        notify_queue: asyncio.Queue[None] = asyncio.Queue()

        def _on_pubsub_message(message: Any) -> None:
            # The payload is {emailAddress, historyId} — we deliberately ignore it
            # and always resume from the durable cursor; see the module docstring.
            message.ack()
            loop.call_soon_threadsafe(notify_queue.put_nowait, None)

        pull_future = subscriber.subscribe(self._subscription_path(), callback=_on_pubsub_message)
        logger.info("gmail_push_listening", user=self.user, subscription=self.subscription)

        renew_task = asyncio.create_task(self._renew_watch_loop())
        try:
            while True:
                await notify_queue.get()
                try:
                    count = await self._handle_notification()
                    if count:
                        logger.info("gmail_push_batch_complete", processed_count=count)
                except Exception as err:  # one bad notification must not kill the listener
                    logger.error("gmail_push_notification_error", error=str(err))
        finally:
            renew_task.cancel()
            pull_future.cancel()
            subscriber.close()
