from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage, OutboundMessage


class DeliveryReceipt(BaseModel):
    """Proof that a channel actually accepted a message for delivery."""

    provider_message_id: str
    delivered_at: datetime


class OutboundChannel(Protocol):
    """A transport that can deliver a reply.

    `send` returns a receipt or raises. There is deliberately no boolean return: a
    caller cannot mistake "I ignored the result" for "it was delivered".
    """

    channel: Channel

    async def send(self, outbound: OutboundMessage) -> DeliveryReceipt: ...
    async def health(self) -> bool: ...


class InboundChannel(Protocol):
    """A transport that can supply new customer messages."""

    channel: Channel

    async def fetch(self) -> list[InboundMessage]: ...
    async def health(self) -> bool: ...
