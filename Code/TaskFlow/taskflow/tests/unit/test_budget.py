"""Budget accounting.

The `budgets` block in providers.yaml was read by no code (BRD NFR-5.2).
"""

import pytest

from taskflow.domain.models import LLMCall
from taskflow.services.cost.service import (
    calculate_cost,
    check_daily_budget,
    daily_spend_limit_usd,
    max_calls_per_message,
    pricing_for,
    total_trace_cost,
)


def test_budget_values_come_from_config():
    assert max_calls_per_message() == 3
    assert daily_spend_limit_usd() == pytest.approx(2.00)


def test_spending_under_the_ceiling_is_allowed():
    status = check_daily_budget(0.10)
    assert status.allowed
    assert status.spent_today == pytest.approx(0.10)


def test_spending_at_or_over_the_ceiling_is_refused():
    at_limit = check_daily_budget(2.00)
    over = check_daily_budget(2.50)
    assert not at_limit.allowed
    assert not over.allowed
    assert "ceiling" in over.reason


def test_every_provider_is_priced():
    """Claude used to be priced 0.0/0.0, so failover silently reported zero spend
    (BRD R-10)."""
    for provider in ("openrouter", "claude"):
        in_rate, out_rate = pricing_for(provider)
        assert in_rate > 0, f"{provider} has no input price"
        assert out_rate > 0, f"{provider} has no output price"


def test_ollama_is_free_because_it_is_local():
    assert pricing_for("ollama") == (0.0, 0.0)


def test_cost_is_computed_from_config_rates():
    in_rate, out_rate = pricing_for("openrouter")
    expected = round(1000 / 1000 * in_rate + 500 / 1000 * out_rate, 6)
    assert calculate_cost("openrouter", 1000, 500) == pytest.approx(expected)


def test_failover_to_claude_records_a_nonzero_cost():
    """The regression: a Claude call used to add $0 to the ledger."""
    assert calculate_cost("claude", 1000, 1000) > 0


def test_total_trace_cost_sums_calls():
    calls = [
        LLMCall(
            purpose="draft",
            provider="openrouter",
            model="m",
            prompt_tokens=100,
            completion_tokens=100,
            cost_usd=0.001,
            latency_ms=1,
        )
        for _ in range(3)
    ]
    assert total_trace_cost(calls) == pytest.approx(0.003)
