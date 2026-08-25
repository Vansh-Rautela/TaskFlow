"""Telegram Channel Connector for TaskFlow.

Implements the ChannelConnector interface for Telegram integration.
Handles fetching messages from Telegram and sending responses via Telegram Bot API.
"""

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from taskflow.adapters.llm.router import ProviderRouter
from taskflow.config.settings import settings
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage, OutboundMessage, DeliveryReceipt
from taskflow.ports.channel import ChannelConnector

logger = structlog.get_logger()


class TelegramMessage(BaseModel):
    """Telegram message structure."""
    message_id: int
    from_user: dict[str, Any]
    chat: dict[str, Any]
    date: int
    text: str | None = None


class TelegramChannelConnector(ChannelConnector):
    """Telegram channel connector for TaskFlow."""

    channel: Channel = Channel.TELEGRAM

    def __init__(self) -> None:
        self._bot_token = settings().telegram_bot_token
        self._chat_id = settings().telegram_chat_id
        self._last_update_id = 0

    async def health(self) -> bool:
        """Check if Telegram bot is properly configured and reachable."""
        if not self._bot_token:
            logger.warning("telegram_health_check_failed", reason="Bot token not configured")
            return False
            
        if not self._chat_id:
            logger.warning("telegram_health_check_failed", reason="Chat ID not configured")
            return False
            
        # Test bot token by getting bot info
        try:
            async with httpx.AsyncClient() as client:
                url = f"https://api.telegram.org/bot{self._bot_token}/getMe"
                response = await client.get(url, timeout=10.0)
                return response.status_code == 200
        except Exception as e:
            logger.warning("telegram_health_check_failed", error=str(e))
            return False

    async def fetch(self) -> list[InboundMessage]:
        """Fetch new messages from Telegram."""
        if not self._bot_token or not self._chat_id:
            logger.warning("telegram_fetch_failed", reason="Bot token or chat ID not configured")
            return []

        try:
            async with httpx.AsyncClient() as client:
                # Get updates since last update ID
                url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
                params = {
                    "offset": self._last_update_id + 1,
                    "limit": 100,
                    "timeout": 30
                }
                
                response = await client.get(url, params=params, timeout=35.0)
                
                if response.status_code != 200:
                    logger.warning("telegram_fetch_failed", status_code=response.status_code)
                    return []
                
                data = response.json()
                updates = data.get("result", [])
                
                messages = []
                for update in updates:
                    # Process message updates
                    if "message" in update:
                        message_data = update["message"]
                        
                        # Only process messages from the configured chat
                        if message_data.get("chat", {}).get("id") == int(self._chat_id):
                            # Update last update ID for next fetch
                            self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                            
                            # Create inbound message
                            inbound_msg = InboundMessage(
                                message_id=str(message_data.get("message_id", "")),
                                dedupe_key=f"tg:{message_data.get('message_id', '')}",
                                tenant_id=settings().taskflow_tenant_id,
                                channel=Channel.TELEGRAM,
                                sender=message_data.get("from", {}).get("username", "unknown"),
                                subject=None,
                                body_text=message_data.get("text", ""),
                                body_redacted=message_data.get("text", ""),
                                provider_message_id=str(message_data.get("message_id", "")),
                                received_at=datetime.fromtimestamp(message_data.get("date", 0))
                            )
                            messages.append(inbound_msg)
                
                return messages
                
        except Exception as e:
            logger.error("telegram_fetch_error", error=str(e))
            return []

    async def send(self, outbound: OutboundMessage) -> DeliveryReceipt:
        """Send an outbound message via Telegram."""
        if not self._bot_token:
            raise ValueError("Telegram bot token not configured")

        try:
            async with httpx.AsyncClient() as client:
                url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
                payload = {
                    "chat_id": self._chat_id,
                    "text": outbound.body_text,
                    "parse_mode": "Markdown"
                }
                
                response = await client.post(url, json=payload, timeout=10.0)
                
                if response.status_code == 200:
                    # Parse response to get message ID
                    response_data = response.json()
                    message_id = response_data.get("result", {}).get("message_id", "")
                    
                    return DeliveryReceipt(
                        provider_message_id=str(message_id),
                        delivered_at=datetime.now()
                    )
                else:
                    logger.warning("telegram_send_failed", status_code=response.status_code)
                    raise RuntimeError(f"Telegram send failed: {response.status_code}")
                    
        except Exception as e:
            logger.error("telegram_send_error", error=str(e))
            raise RuntimeError(f"Telegram send error: {e}")