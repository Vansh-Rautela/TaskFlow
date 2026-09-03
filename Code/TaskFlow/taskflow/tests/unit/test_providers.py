"""Unit tests for LLM Providers, Cost Tracking, and Router Failover."""

import pytest
from pydantic import BaseModel

from taskflow.adapters.llm.router import ProviderRouter
from taskflow.domain.errors import AllProvidersFailed, ProviderError
from taskflow.domain.models import DraftOutput, LLMCall
from taskflow.ports.llm import LLMResponse, ProviderCapabilities
from taskflow.services.cost.service import calculate_cost, total_trace_cost


class MockSuccessProvider:
    name: str = "mock_cloud"
    capabilities = ProviderCapabilities(
        supports_json_schema=True, supports_tools=True, max_context=100000
    )

    async def health(self) -> bool:
        return True

    async def complete(
        self, *, system: str, user: str, model: str, schema: type[BaseModel] | None = None, **kwargs
    ) -> LLMResponse:
        draft = DraftOutput(
            response_text="Hello from mock cloud provider [chunk-001]",
            citations=[{"chunk_id": "chunk-001", "doc_title": "Doc"}],
            tone="friendly",
            complexity="simple",
            draft_confidence=0.9,
        )
        call = LLMCall(
            purpose="complete",
            provider=self.name,
            model=model,
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.0005,
            latency_ms=150,
        )
        return LLMResponse(text=draft.model_dump_json(), call=call)


class MockFailingProvider:
    name: str = "mock_failing"
    capabilities = ProviderCapabilities(
        supports_json_schema=True, supports_tools=True, max_context=100000
    )

    async def health(self) -> bool:
        return False

    async def complete(
        self, *, system: str, user: str, model: str, schema: type[BaseModel] | None = None, **kwargs
    ) -> LLMResponse:
        raise ProviderError("Simulated primary provider outage")


class MockFallbackProvider:
    name: str = "mock_local"
    capabilities = ProviderCapabilities(
        supports_json_schema=True, supports_tools=False, max_context=32000
    )

    async def health(self) -> bool:
        return True

    async def complete(
        self, *, system: str, user: str, model: str, schema: type[BaseModel] | None = None, **kwargs
    ) -> LLMResponse:
        draft = DraftOutput(
            response_text="Hello from local fallback provider [chunk-001]",
            citations=[{"chunk_id": "chunk-001", "doc_title": "Doc"}],
            tone="neutral",
            complexity="simple",
            draft_confidence=0.85,
        )
        call = LLMCall(
            purpose="complete",
            provider=self.name,
            model=model,
            prompt_tokens=80,
            completion_tokens=40,
            cost_usd=0.0,
            latency_ms=200,
        )
        return LLMResponse(text=draft.model_dump_json(), call=call)


def test_cost_calculation():
    """Verify USD cost calculation and trace cost summation."""
    cost = calculate_cost("claude", prompt_tokens=1000, completion_tokens=500)
    assert isinstance(cost, float)

    calls = [
        LLMCall(
            purpose="draft",
            provider="claude",
            model="test",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.001,
            latency_ms=100,
        ),
        LLMCall(
            purpose="grounding",
            provider="claude",
            model="test",
            prompt_tokens=200,
            completion_tokens=100,
            cost_usd=0.002,
            latency_ms=150,
        ),
    ]
    assert total_trace_cost(calls) == 0.003


@pytest.mark.asyncio
async def test_router_primary_success(monkeypatch):
    """Primary provider succeeds on first attempt."""
    monkeypatch.setattr(
        "taskflow.adapters.llm.router.provider_priority", lambda: ["claude", "ollama"]
    )
    monkeypatch.setattr("taskflow.adapters.llm.router.model_for", lambda p, pur: "test-model")

    providers = {
        "claude": MockSuccessProvider(),
        "ollama": MockFallbackProvider(),
    }
    router = ProviderRouter(providers=providers)

    output, call = await router.complete_structured(
        purpose="draft",
        system="system",
        user="user",
        schema=DraftOutput,
    )

    assert isinstance(output, DraftOutput)
    assert call.provider == "claude"
    assert call.attempts == 1
    assert not call.failed_over


@pytest.mark.asyncio
async def test_router_failover(monkeypatch):
    """Primary provider fails; router automatically falls back to secondary provider."""
    monkeypatch.setattr(
        "taskflow.adapters.llm.router.provider_priority", lambda: ["claude", "ollama"]
    )
    monkeypatch.setattr("taskflow.adapters.llm.router.model_for", lambda p, pur: "test-model")

    providers = {
        "claude": MockFailingProvider(),
        "ollama": MockFallbackProvider(),
    }
    router = ProviderRouter(providers=providers)

    output, call = await router.complete_structured(
        purpose="draft",
        system="system",
        user="user",
        schema=DraftOutput,
    )

    assert isinstance(output, DraftOutput)
    assert call.provider == "ollama"
    assert call.attempts == 2
    assert call.failed_over


@pytest.mark.asyncio
async def test_router_all_failed(monkeypatch):
    """All providers fail; router raises AllProvidersFailed."""
    monkeypatch.setattr(
        "taskflow.adapters.llm.router.provider_priority", lambda: ["claude", "ollama"]
    )
    monkeypatch.setattr("taskflow.adapters.llm.router.model_for", lambda p, pur: "test-model")

    providers = {
        "claude": MockFailingProvider(),
        "ollama": MockFailingProvider(),
    }
    router = ProviderRouter(providers=providers)

    with pytest.raises(AllProvidersFailed):
        await router.complete_structured(
            purpose="draft",
            system="system",
            user="user",
            schema=DraftOutput,
        )


# --------------------------------------------------------------------------
# Regressions for the router's failover behaviour
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_missing_a_model_is_skipped_not_fatal():
    """BRD R-8.

    Only OpenRouter declared a `classification` model, so `model_for` raised
    KeyError — which the router's `except (TransientError, ProviderError,
    SchemaError)` did not catch. The exception escaped the loop, so classification
    could never fail over and always collapsed to the keyword classifier.
    """
    from taskflow.domain.models import ClassificationOutput

    router = ProviderRouter(
        providers={"openrouter": MockFailingProvider(), "ollama": MockClassifierProvider()}
    )

    output, call = await router.complete_structured(
        purpose="classification",
        system="s",
        user="I was double charged",
        schema=ClassificationOutput,
    )

    assert call.provider == "ollama", "the chain stopped at the provider that failed"
    assert output.intent is not None


@pytest.mark.asyncio
async def test_every_provider_declares_a_model_for_every_purpose():
    """Guards the config that makes the test above pass in production."""
    from taskflow.config.settings import model_for, providers_config

    configured = providers_config()["providers"]
    for purpose in ("classification", "draft", "grounding"):
        for provider in configured:
            assert model_for(provider, purpose), f"{provider} has no {purpose} model"


@pytest.mark.asyncio
async def test_an_untranslated_adapter_error_still_fails_over(monkeypatch):
    """An adapter that leaks a raw exception is a defect, but it must not take the
    whole provider chain down with it."""
    monkeypatch.setattr(
        "taskflow.adapters.llm.router.provider_priority", lambda: ["claude", "ollama"]
    )
    monkeypatch.setattr("taskflow.adapters.llm.router.model_for", lambda p, pur: "test-model")

    class LeakyProvider:
        name = "leaky"
        capabilities = ProviderCapabilities(
            supports_json_schema=True, supports_tools=False, max_context=1000
        )

        async def health(self) -> bool:
            return True

        async def complete(self, **kwargs):
            raise RuntimeError("an SDK error nobody translated")

    router = ProviderRouter(providers={"claude": LeakyProvider(), "ollama": MockFallbackProvider()})
    _output, call = await router.complete_structured(
        purpose="draft", system="s", user="u", schema=DraftOutput
    )
    assert call.provider == "ollama"


class MockClassifierProvider:
    """Returns a valid ClassificationOutput regardless of the prompt."""

    name: str = "mock_classifier"
    capabilities = ProviderCapabilities(
        supports_json_schema=True, supports_tools=False, max_context=32000
    )

    async def health(self) -> bool:
        return True

    async def complete(self, *, system: str, user: str, model: str, schema=None, **kwargs):
        from taskflow.domain.enums import Intent
        from taskflow.domain.models import ClassificationOutput

        output = ClassificationOutput(intent=Intent.BILLING, confidence=0.9, reasoning="mock")
        return LLMResponse(
            text=output.model_dump_json(),
            call=LLMCall(
                purpose="classification",
                provider=self.name,
                model=model,
                prompt_tokens=10,
                completion_tokens=5,
                cost_usd=0.0,
                latency_ms=5,
            ),
        )
