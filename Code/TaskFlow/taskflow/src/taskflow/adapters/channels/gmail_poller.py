"""Gmail IMAP Inbound Poller Adapter for TaskFlow.

Polls imap.gmail.com:993 using GMAIL_USER and GMAIL_APP_PASSWORD configured in .env.
Fetches unseen inbound messages, parses email headers and body text, and posts each
one to the API's `POST /messages` endpoint rather than running the pipeline
in-process.

That indirection matters: the API already holds the embedded Qdrant store's
exclusive file lock (`data/qdrant/.lock`) for the lifetime of the process. A second
process opening `QdrantClient` against the same path — this poller, if it built its
own `Deps` and called `run_pipeline` directly — crashes immediately with "Storage
folder already accessed by another instance". Routing every inbound message through
the already-running API sidesteps that: this process never touches the vector
store, so only one process ever holds the lock, however many inbound channels run
alongside it. `gmail_push.py` (Track C) uses this same client.
"""

import asyncio
import email
import imaplib
import uuid

import structlog

from taskflow.adapters.channels._api_client import post_inbound_message
from taskflow.adapters.channels._email_parsing import (
    clean_email_address,
    decode_str,
    extract_body,
    is_ignorable_sender,
    strip_quoted_reply,
)
from taskflow.config.settings import app_config, settings
from taskflow.domain.enums import Channel
from taskflow.domain.errors import ProviderError, TransientError

logger = structlog.get_logger()


class GmailIMAPPoller:
    """Polls Gmail IMAP for unseen emails and posts each to the TaskFlow API."""

    def __init__(
        self,
        api_base_url: str | None = None,
        api_token: str | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        self.api_base_url = api_base_url or settings().taskflow_api_base_url
        self.api_token = api_token or settings().taskflow_api_token
        self.poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else float(app_config().get("pollers", {}).get("gmail_interval_s", 30))
        )
        self.user = settings().gmail_user
        self.password = settings().gmail_app_password

    def _connect(self) -> imaplib.IMAP4_SSL:
        if not self.user or not self.password:
            raise ValueError("GMAIL_USER and GMAIL_APP_PASSWORD must be configured in .env")

        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(self.user, self.password)
        mail.select(settings().gmail_label or "INBOX")
        return mail

    async def poll_once(self) -> int:
        """Connect to IMAP, fetch UNSEEN emails, and run through pipeline."""
        if not self.user or not self.password:
            logger.warning(
                "gmail_poller_skipped", reason="Missing GMAIL_USER or GMAIL_APP_PASSWORD"
            )
            return 0

        processed_count = 0
        loop = asyncio.get_running_loop()

        def _fetch_unseen_emails() -> list[tuple[str, str, str, str]]:
            try:
                mail = self._connect()
                status, response = mail.search(None, "UNSEEN")
                if status != "OK" or not response[0]:
                    mail.logout()
                    return []

                msg_nums = response[0].split()
                fetched = []

                for num in msg_nums:
                    status, data = mail.fetch(num, "(RFC822)")
                    if status != "OK" or not data or not data[0]:
                        continue

                    raw_email = data[0][1]
                    if not isinstance(raw_email, bytes):
                        logger.warning("gmail_poller_unexpected_fetch_payload", num=str(num))
                        continue
                    msg_obj = email.message_from_bytes(raw_email)

                    subject = decode_str(msg_obj.get("Subject"))
                    sender = clean_email_address(decode_str(msg_obj.get("From")))
                    message_id_hdr = (
                        decode_str(msg_obj.get("Message-ID")) or f"gmail-{uuid.uuid4().hex[:8]}"
                    )
                    body = extract_body(msg_obj)

                    # Mark email as read / SEEN
                    mail.store(num, "+FLAGS", "\\Seen")

                    if is_ignorable_sender(
                        sender=sender,
                        subject=subject,
                        user_email=self.user or "",
                        ops_email=settings().ops_email or "",
                    ):
                        logger.info(
                            "gmail_poller_ignoring_system_email", sender=sender, subject=subject
                        )
                        continue

                    if body or subject:
                        fetched.append((message_id_hdr, sender, subject, body))

                mail.logout()
                return fetched
            except Exception as err:
                logger.warning("gmail_poller_fetch_error", error=str(err))
                return []

        fetched_messages = await loop.run_in_executor(None, _fetch_unseen_emails)

        for msg_id_hdr, sender, subject, body in fetched_messages:
            body_text = strip_quoted_reply(body) or subject or "No body text provided"

            try:
                logger.info(
                    "gmail_processing_inbound",
                    sender=sender,
                    subject=subject,
                    message_id=msg_id_hdr,
                )
                # message_id is the RFC-822 Message-ID header: the API derives the
                # dedupe key from it, so redelivery of the same mail produces the
                # same key and the pipeline's dedupe gate catches it.
                result = await post_inbound_message(
                    api_base_url=self.api_base_url,
                    api_token=self.api_token,
                    message_id=msg_id_hdr,
                    tenant_id=settings().taskflow_tenant_id or "taskflow-demo",
                    channel=Channel.EMAIL.value,
                    sender=sender,
                    subject=subject,
                    body_text=body_text,
                )
                processed_count += 1
                logger.info(
                    "gmail_processed_inbound",
                    action=result.get("action"),
                    reason=result.get("reason"),
                    message_id=msg_id_hdr,
                )
            except (TransientError, ProviderError) as err:
                logger.error("gmail_api_post_failed", message_id=msg_id_hdr, error=str(err))

        return processed_count

    async def run_poller_loop(self) -> None:
        """Run continuous polling loop until cancelled."""
        logger.info("gmail_poller_starting", user=self.user, interval_s=self.poll_interval_s)
        while True:
            try:
                count = await self.poll_once()
                if count > 0:
                    logger.info("gmail_poller_batch_complete", processed_count=count)
            except Exception as err:
                logger.error("gmail_poller_loop_error", error=str(err))

            await asyncio.sleep(self.poll_interval_s)
