"""Gmail Push message mapping.

The live path (Watch registration, Pub/Sub pull, `history.list`/`messages.get`)
needs a real Gmail/Pub-Sub connection and isn't exercised here — see
docs/14_GMAIL_PUSH_SETUP.md. What's covered is the one part of that path that's
pure and deterministic: turning one fetched raw RFC-822 message into what gets
posted to the API, and which messages get skipped.
"""

from email.message import EmailMessage

from taskflow.adapters.channels.gmail_push import gmail_message_to_inbound_fields

USER_EMAIL = "support@taskflow.dev"


def _raw(msg: EmailMessage) -> bytes:
    return bytes(msg)


def test_a_normal_message_is_mapped_to_post_fields():
    msg = EmailMessage()
    msg["Subject"] = "Refund request"
    msg["From"] = "Jane Doe <jane@acme.com>"
    msg["Message-ID"] = "<abc123@mail.gmail.com>"
    msg.set_content("Can I get a refund for my unused seats?")

    fields = gmail_message_to_inbound_fields(_raw(msg), user_email=USER_EMAIL)

    assert fields == {
        "message_id": "<abc123@mail.gmail.com>",
        "sender": "jane@acme.com",
        "subject": "Refund request",
        "body_text": "Can I get a refund for my unused seats?",
    }


def test_a_message_with_no_message_id_is_skipped():
    """Without a stable Message-ID there is nothing to dedupe redelivery against."""
    msg = EmailMessage()
    msg["Subject"] = "Question"
    msg["From"] = "jane@acme.com"
    msg.set_content("Hello.")

    assert gmail_message_to_inbound_fields(_raw(msg), user_email=USER_EMAIL) is None


def test_a_self_sent_alert_email_is_skipped():
    msg = EmailMessage()
    msg["Subject"] = "[TaskFlow Alert] New review item"
    msg["From"] = USER_EMAIL
    msg["Message-ID"] = "<alert-1@taskflow.dev>"
    msg.set_content("A message needs review.")

    assert gmail_message_to_inbound_fields(_raw(msg), user_email=USER_EMAIL) is None


def test_a_mailer_daemon_bounce_is_skipped():
    msg = EmailMessage()
    msg["Subject"] = "Delivery Status Notification (Failure)"
    msg["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    msg["Message-ID"] = "<bounce-1@googlemail.com>"
    msg.set_content("Undelivered.")

    assert gmail_message_to_inbound_fields(_raw(msg), user_email=USER_EMAIL) is None


def test_quoted_reply_history_is_stripped_from_the_mapped_body():
    msg = EmailMessage()
    msg["Subject"] = "Re: Refund request"
    msg["From"] = "jane@acme.com"
    msg["Message-ID"] = "<reply-1@mail.gmail.com>"
    msg.set_content(
        "Still waiting on this.\n\n"
        "On Mon, 3 Feb 2026 at 09:14, Support <s@taskflow.dev> wrote:\n"
        "> We're looking into it.\n"
    )

    fields = gmail_message_to_inbound_fields(_raw(msg), user_email=USER_EMAIL)

    assert fields is not None
    assert fields["body_text"] == "Still waiting on this."


def test_an_ops_sender_is_also_skipped_when_configured():
    msg = EmailMessage()
    msg["Subject"] = "FYI"
    msg["From"] = "ops@taskflow.dev"
    msg["Message-ID"] = "<ops-1@taskflow.dev>"
    msg.set_content("Internal note.")

    fields = gmail_message_to_inbound_fields(
        _raw(msg), user_email=USER_EMAIL, ops_email="ops@taskflow.dev"
    )

    assert fields is None
