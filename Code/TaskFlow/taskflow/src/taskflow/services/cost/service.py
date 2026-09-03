"""Cost accounting and budget enforcement.

Pricing lives in `config/providers.yaml` and nowhere else. Adapters used to also
carry prices in their `capabilities`, which meant two numbers could disagree about
what a call cost; the adapters now read from here.
"""

import structlog

from taskflow.config.settings import budgets, providers_config
from taskflow.domain.models import LLMCall

logger = structlog.get_logger()


def pricing_for(provider: str) -> tuple[float, float]:
    """(input $/1k tokens, output $/1k tokens) for a provider."""
    pricing = providers_config().get("providers", {}).get(provider, {}).get("pricing", {})
    return float(pricing.get("in_per_1k", 0.0)), float(pricing.get("out_per_1k", 0.0))


def calculate_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Cost of one call in USD."""
    in_rate, out_rate = pricing_for(provider)
    return round((prompt_tokens / 1000.0) * in_rate + (completion_tokens / 1000.0) * out_rate, 6)


def total_trace_cost(calls: list[LLMCall]) -> float:
    """Total USD across a trace's LLM calls."""
    return round(sum(c.cost_usd for c in calls), 6)


def max_calls_per_message() -> int:
    """`budgets.max_llm_calls_per_message` from providers.yaml."""
    return int(budgets().get("max_llm_calls_per_message", 3))


def daily_spend_limit_usd() -> float:
    """`budgets.daily_spend_usd` from providers.yaml. 0 disables the ceiling."""
    return float(budgets().get("daily_spend_usd", 0.0))


def spend_alert_threshold_usd() -> float:
    """The spend at which to warn, from `budgets.alert_at_pct`."""
    return daily_spend_limit_usd() * float(budgets().get("alert_at_pct", 80)) / 100.0


class BudgetStatus:
    """Outcome of a budget check, so the caller can act on *why* it was refused."""

    def __init__(self, allowed: bool, reason: str, spent_today: float) -> None:
        self.allowed = allowed
        self.reason = reason
        self.spent_today = spent_today


def check_daily_budget(spent_today_usd: float) -> BudgetStatus:
    """Decide whether another LLM call is within the configured daily ceiling.

    Returning a status rather than raising lets the pipeline degrade to its template
    path — refusing to spend is not the same as failing to answer.
    """
    limit = daily_spend_limit_usd()
    if limit <= 0:
        return BudgetStatus(True, "no daily limit configured", spent_today_usd)

    if spent_today_usd >= limit:
        return BudgetStatus(
            False,
            f"daily spend ${spent_today_usd:.4f} has reached the ${limit:.2f} ceiling",
            spent_today_usd,
        )

    alert_at = spend_alert_threshold_usd()
    if spent_today_usd >= alert_at:
        logger.warning(
            "daily_spend_approaching_limit",
            spent_usd=round(spent_today_usd, 4),
            limit_usd=limit,
            alert_at_usd=round(alert_at, 4),
        )

    return BudgetStatus(True, "within budget", spent_today_usd)
