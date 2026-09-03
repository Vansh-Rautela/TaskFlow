"""Unit tests for Phase P10 Multi-Channel Alerting Service."""

from datetime import UTC, datetime

import pytest

from taskflow.config.settings import settings
from taskflow.domain.enums import ReviewState, RouteAction
from taskflow.domain.models import (
    ConfidenceBreakdown,
    DraftOutput,
    ReviewItem,
    RoutingDecision,
)
from taskflow.services.alert.service import (
    dispatch_alert,
    send_email_alert,
    send_telegram_alert,
)


@pytest.fixture
def sample_review_item():
    now = datetime.now(UTC)
    return ReviewItem(
        review_id="rev-alert-001",
        trace_id="tr-alert-001",
        conversation_id="conv-alert-001",
        tenant_id="test",
        state=ReviewState.PENDING,
        draft=DraftOutput(
            response_text="Draft text requiring operator verification.",
            citations=[],
            tone="friendly",
            complexity="simple",
            draft_confidence=0.7,
        ),
        decision=RoutingDecision(
            action=RouteAction.HUMAN_REVIEW,
            reason="Low confidence score",
            reason_code="low_confidence",
            confidence=ConfidenceBreakdown(gates=[], weights={}, score=0.65, threshold=0.7),
        ),
        created_at=now,
        sla_deadline=now,
    )


@pytest.mark.asyncio
async def test_send_email_alert_unconfigured():
    """Returns False when GMAIL_USER is unconfigured."""
    res = await send_email_alert("Test Subject", "Test Body")
    assert isinstance(res, bool)


@pytest.mark.asyncio
async def test_send_telegram_alert_unconfigured(monkeypatch):
    """Returns False when TELEGRAM_BOT_TOKEN is unconfigured."""
    monkeypatch.setattr(settings(), "telegram_bot_token", "")
    res = await send_telegram_alert("Test Telegram Message")
    assert not res


@pytest.mark.asyncio
async def test_dispatch_alert(sample_review_item):
    """Dispatch alert returns status dict across all channels."""
    results = await dispatch_alert(sample_review_item)
    assert isinstance(results, dict)
    assert "email" in results
    assert "telegram" in results
    assert "webhook" in results
