"""Gmail IMAP Inbound Poller Adapter for TaskFlow.

Polls imap.gmail.com:993 using GMAIL_USER and GMAIL_APP_PASSWORD configured in .env.
Fetches unseen inbound messages, parses email headers and body text, constructs
InboundMessage domain objects, and runs them through the TaskFlow pipeline.
"""

import asyncio
import email
import imaplib
import re
import uuid
from datetime import UTC, datetime
from email.header import decode_header
from typing import Any

import structlog

from taskflow.config.settings import settings
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import Deps, run_pipeline

logger = structlog.get_logger()


def _decode_str(header_val: Any) -> str:
    if not header_val:
        return ""
    if isinstance(header_val, bytes):
        return header_val.decode("utf-8", errors="ignore")
    decoded_parts = decode_header(header_val)
    res = []
    for content, encoding in decoded_parts:
        if isinstance(content, bytes):
            enc = encoding or "utf-8"
            res.append(content.decode(enc, errors="ignore"))
        else:
            res.append(str(content))
    return "".join(res)


def _clean_email_address(raw_sender: str) -> str:
    match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", raw_sender)
    if match:
        return match.group(0)
    return raw_sender.strip()


def _extract_body(msg_obj: email.message.Message) -> str:
    body_parts = []
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode("utf-8", errors="ignore"))
    else:
        payload = msg_obj.get_payload(decode=True)
        if payload:
            body_parts.append(payload.decode("utf-8", errors="ignore"))

    return "\n".join(body_parts).strip()


class GmailIMAPPoller:
    """Polls Gmail IMAP box for unseen emails and processes them via TaskFlow pipeline."""

    def __init__(self, deps: Deps, poll_interval_s: float = 15.0) -> None:
        self.deps = deps
        self.poll_interval_s = poll_interval_s
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
            logger.warning("gmail_poller_skipped", reason="Missing GMAIL_USER or GMAIL_APP_PASSWORD")
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
                    msg_obj = email.message_from_bytes(raw_email)

                    subject = _decode_str(msg_obj.get("Subject"))
                    sender = _clean_email_address(_decode_str(msg_obj.get("From")))
                    message_id_hdr = _decode_str(msg_obj.get("Message-ID")) or f"gmail-{uuid.uuid4().hex[:8]}"
                    body = _extract_body(msg_obj)

                    # Mark email as read / SEEN
                    mail.store(num, "+FLAGS", "\\Seen")

                    if body or subject:
                        fetched.append((message_id_hdr, sender, subject, body))

                mail.logout()
                return fetched
            except Exception as err:
                logger.warning("gmail_poller_fetch_error", error=str(err))
                return []

        fetched_messages = await loop.run_in_executor(None, _fetch_unseen_emails)

        for msg_id_hdr, sender, subject, body in fetched_messages:
            msg_id = f"gmail-{uuid.uuid4().hex[:8]}"
            dedupe_key = f"email:{msg_id_hdr}"

            inbound = InboundMessage(
                message_id=msg_id,
                dedupe_key=dedupe_key,
                tenant_id=settings().taskflow_tenant_id or "taskflow-demo",
                channel=Channel.EMAIL,
                sender=sender,
                subject=subject,
                body_text=body or subject or "No body text provided",
                body_redacted=body or subject or "No body text provided",
                provider_message_id=msg_id_hdr,
                received_at=datetime.now(UTC),
            )

            try:
                logger.info("gmail_processing_inbound", sender=sender, subject=subject, message_id=msg_id)
                decision = await run_pipeline(inbound, self.deps)
                processed_count += 1
                logger.info("gmail_processed_inbound", action=decision.action.value, reason=decision.reason)
            except Exception as err:
                logger.error("gmail_pipeline_error", message_id=msg_id, error=str(err))

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
