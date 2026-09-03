"""MIME/header parsing shared by the IMAP poller and the Gmail Push adapter.

Both channels arrive at the same shape (sender, subject, plain-text body) from two
different transports — IMAP hands over a raw RFC-822 message, the Gmail API hands
over a base64url-encoded MIME payload — but once decoded into an `email.message.Message`
the parsing is identical. Pulled out here so it exists once.
"""

import email
import re
from email.header import decode_header
from typing import Any

# "On Mon, 3 Feb 2026 at 09:14, Someone <a@b.com> wrote:" and the Outlook divider.
_QUOTE_HEADER = re.compile(
    r"^(On .{5,120} wrote:|-{2,}\s*Original Message\s*-{2,}|_{5,}|From:\s)", re.IGNORECASE
)


def decode_str(header_val: Any) -> str:
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


def clean_email_address(raw_sender: str) -> str:
    match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", raw_sender)
    if match:
        return match.group(0)
    return raw_sender.strip()


def _decode_payload(part: email.message.Message) -> str | None:
    """Decoded text of one MIME part, or None when it carries no bytes."""
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return None
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="ignore")


def extract_body(msg_obj: email.message.Message) -> str:
    """Plain-text body, attachments excluded."""
    body_parts: list[str] = []
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            disposition = str(part.get("Content-Disposition"))
            if part.get_content_type() == "text/plain" and "attachment" not in disposition:
                text = _decode_payload(part)
                if text:
                    body_parts.append(text)
    else:
        text = _decode_payload(msg_obj)
        if text:
            body_parts.append(text)

    return "\n".join(body_parts).strip()


def is_ignorable_sender(*, sender: str, subject: str, user_email: str, ops_email: str = "") -> bool:
    """True for self-sent alert mail, mailer-daemons, and system notifications.

    Both inbound channels share this: without it, every alert TaskFlow itself
    sends to the ops inbox would loop back in as a new inbound message.
    """
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    user_email_lower = user_email.lower()
    ops_email_lower = ops_email.lower()

    is_self_sender = bool(
        (user_email_lower and user_email_lower in sender_lower)
        or (ops_email_lower and ops_email_lower in sender_lower)
    )
    is_system_sender = any(
        sys_kw in sender_lower for sys_kw in ["mailer-daemon", "postmaster", "noreply", "no-reply"]
    )
    is_alert_subject = (
        "[taskflow alert]" in subject_lower
        or "taskflow alert" in subject_lower
        or "delivery status notification" in subject_lower
    )
    return is_self_sender or is_system_sender or is_alert_subject


def strip_quoted_reply(body: str) -> str:
    """Drop quoted history so the pipeline classifies the new text, not the thread.

    InboundMessage documents body_text as "quoted replies stripped"; nothing was
    doing the stripping, so every reply in a thread re-fed the whole conversation
    into retrieval and the drafting prompt.
    """
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _QUOTE_HEADER.match(stripped):
            break
        lines.append(line)
    return "\n".join(lines).strip() or body.strip()
