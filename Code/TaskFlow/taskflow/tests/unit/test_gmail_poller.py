"""Gmail poller message construction.

The poller's IMAP conversation is not exercised here — that needs a real mailbox.
What is covered is everything the poller decides *about* a message once it has the
bytes: the dedupe key, the body it hands the pipeline, and which senders it ignores.
"""

import email
from email.message import EmailMessage

import pytest

from taskflow.adapters.channels._email_parsing import (
    clean_email_address,
    decode_str,
    extract_body,
    strip_quoted_reply,
)
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage


def test_dedupe_key_is_derived_from_the_rfc822_message_id():
    """The poller used to build its key from a fresh uuid, so redelivery of the same
    mail produced a new key every time and deduplication could never fire."""
    header = "<CAF=abc123@mail.gmail.com>"
    first = InboundMessage.make_dedupe_key(Channel.EMAIL, header)
    second = InboundMessage.make_dedupe_key(Channel.EMAIL, header)

    assert first == second
    assert first == f"email:{header}"


def test_quoted_reply_history_is_stripped():
    """`InboundMessage` documents body_text as "quoted replies stripped"; nothing was
    stripping it, so every reply re-fed the whole thread into retrieval and the prompt."""
    body = (
        "The webhook is still timing out.\n"
        "\n"
        "On Mon, 3 Feb 2026 at 09:14, Support <s@taskflow.dev> wrote:\n"
        "> Have you tried raising the timeout?\n"
        "> Let us know.\n"
    )
    assert strip_quoted_reply(body) == "The webhook is still timing out."


@pytest.mark.parametrize(
    "divider",
    ["----- Original Message -----", "______________________________", "From: someone@x.com"],
)
def test_other_quote_dividers_are_stripped(divider):
    body = f"My actual question.\n\n{divider}\nold content here"
    assert strip_quoted_reply(body) == "My actual question."


def test_a_message_that_is_entirely_quoted_keeps_its_text():
    """Better to classify a quoted-only reply than to hand the pipeline an empty body."""
    body = "> just a quote\n> and another"
    assert strip_quoted_reply(body) == body.strip()


def test_unquoted_body_is_untouched():
    body = "Hello, I have a question about my invoice."
    assert strip_quoted_reply(body) == body


def test_sender_address_is_extracted_from_a_display_name():
    assert clean_email_address("Jane Doe <jane@acme.com>") == "jane@acme.com"
    assert clean_email_address("bob@acme.com") == "bob@acme.com"


def test_encoded_subject_is_decoded():
    assert decode_str("=?utf-8?q?Refund_request?=") == "Refund request"
    assert decode_str(None) == ""


def test_plaintext_body_is_extracted_from_a_multipart_message():
    msg = EmailMessage()
    msg["Subject"] = "Question"
    msg.set_content("The plain text part.")
    msg.add_alternative("<p>The HTML part.</p>", subtype="html")

    extracted = extract_body(email.message_from_bytes(bytes(msg)))
    assert "The plain text part." in extracted
    assert "HTML part" not in extracted


def test_attachments_are_not_treated_as_body_text():
    msg = EmailMessage()
    msg.set_content("Real question here.")
    msg.add_attachment(b"binary", maintype="application", subtype="octet-stream", filename="x.bin")

    assert extract_body(email.message_from_bytes(bytes(msg))).strip() == "Real question here."


def test_a_body_with_no_text_part_yields_empty_string():
    msg = EmailMessage()
    msg.add_attachment(
        b"only-an-attachment", maintype="application", subtype="pdf", filename="a.pdf"
    )
    assert extract_body(email.message_from_bytes(bytes(msg))) == ""
