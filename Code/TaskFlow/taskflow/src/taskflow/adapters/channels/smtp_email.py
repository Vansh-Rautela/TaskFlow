"""Gmail SMTP outbound email adapter.

The only thing in the system that actually delivers a customer reply. It either
returns a receipt or raises — it never reports a send it did not make.
"""

import smtplib
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

import structlog

from taskflow.config.settings import settings
from taskflow.domain.enums import Channel
from taskflow.domain.errors import ConfigError, TransientError
from taskflow.domain.models import OutboundMessage
from taskflow.ports.channel import DeliveryReceipt

logger = structlog.get_logger()

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class SMTPEmailChannel:
    """Satisfies the outbound half of ports.channel.ChannelConnector."""

    channel: Channel = Channel.EMAIL

    def __init__(self, user: str | None = None, password: str | None = None) -> None:
        self._user = user if user is not None else settings().gmail_user
        self._password = password if password is not None else settings().gmail_app_password

    async def health(self) -> bool:
        return bool(self._user and self._password)

    async def send(self, outbound: OutboundMessage) -> DeliveryReceipt:
        """Send one message. Raises on every failure; returns a receipt on success.

        Raising rather than returning False is deliberate: the caller cannot then
        accidentally treat a failure as a success by ignoring a return value.
        """
        if not self._user or not self._password:
            raise ConfigError(
                "SMTP delivery is not configured (GMAIL_USER / GMAIL_APP_PASSWORD unset)"
            )
        if not outbound.recipient or "@" not in outbound.recipient:
            raise ConfigError(
                f"outbound message has no deliverable recipient: {outbound.recipient!r}"
            )

        message_id = make_msgid(domain=self._user.split("@")[-1])
        mime = MIMEMultipart()
        mime["From"] = self._user
        mime["To"] = outbound.recipient
        mime["Subject"] = outbound.subject or "Re: TaskFlow Support Request"
        mime["Message-ID"] = message_id
        for header, value in outbound.reply_headers.items():
            mime[header] = value
        mime.attach(MIMEText(outbound.body_text, "plain", "utf-8"))

        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(self._user, self._password)
                refused = server.send_message(mime)
        except smtplib.SMTPAuthenticationError as err:
            # Bad credentials will not fix themselves on retry.
            raise ConfigError(f"SMTP authentication rejected: {err}") from err
        except (
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
            TimeoutError,
            OSError,
        ) as err:
            raise TransientError(f"SMTP connection failed: {err}") from err
        except smtplib.SMTPRecipientsRefused as err:
            raise ConfigError(f"SMTP refused every recipient: {err}") from err
        except smtplib.SMTPException as err:
            raise TransientError(f"SMTP error: {err}") from err

        if refused:
            raise TransientError(f"SMTP refused recipients: {refused}")

        logger.info(
            "smtp_message_delivered",
            outbound_id=outbound.outbound_id,
            recipient=outbound.recipient,
            provider_message_id=message_id,
        )
        return DeliveryReceipt(provider_message_id=message_id, delivered_at=datetime.now(UTC))
