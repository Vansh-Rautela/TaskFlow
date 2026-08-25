"""Unit tests for Phase P6 Intent Classification."""

import pytest
from pydantic import BaseModel

from taskflow.adapters.llm.router import ProviderRouter
from taskflow.domain.enums import Intent
from taskflow.domain.errors import ProviderError
from taskflow.domain.models import ClassificationOutput, LLMCall
from taskflow.ports.llm import LLMResponse, ProviderCapabilities
from taskflow.services.classify.service import classify_intent


class MockClassifierProvider:
    name: str = "mock_classifier"
    capabilities = ProviderCapabilities(
        supports_json_schema=True, supports_tools=False, max_context=32000
    )

    async def health(self) -> bool:
        return True

    async def complete(
        self, *, system: str, user: str, model: str, schema: type[BaseModel] | None = None, **kwargs
    ) -> LLMResponse:
        output = ClassificationOutput(
            intent=Intent.BILLING,
            confidence=0.92,
            reasoning="User references double charging on invoice",
        )
        call = LLMCall(
            purpose="classification",
            provider=self.name,
            model=model,
            prompt_tokens=50,
            completion_tokens=20,
            cost_usd=0.0001,
            latency_ms=80,
        )
        return LLMResponse(text=output.model_dump_json(), call=call)


class MockFailingClassifierProvider:
    name: str = "mock_failing"
    capabilities = ProviderCapabilities(
        supports_json_schema=True, supports_tools=False, max_context=32000
    )

    async def health(self) -> bool:
        return False

    async def complete(
        self, *, system: str, user: str, model: str, schema: type[BaseModel] | None = None, **kwargs
    ) -> LLMResponse:
        raise ProviderError("Classifier LLM provider unreachable")


@pytest.mark.asyncio
async def test_structured_llm_classification(monkeypatch):
    """Structured LLM router classifies intent with confidence score."""
    monkeypatch.setattr("taskflow.adapters.llm.router.provider_priority", lambda: ["claude"])
    monkeypatch.setattr("taskflow.adapters.llm.router.model_for", lambda p, pur: "test-model")

    router = ProviderRouter(providers={"claude": MockClassifierProvider()})
    intent, confidence, _call = await classify_intent(
        "I was double charged on my invoice", router=router
    )

    assert intent == Intent.BILLING
    assert confidence == 0.92


@pytest.mark.asyncio
async def test_fallback_classification():
    """Keyword fallback classification when router is None."""
    intent, confidence, _call = await classify_intent("Can I get a refund?", router=None)
    assert intent == Intent.REFUND
    assert confidence == 0.95


@pytest.mark.asyncio
async def test_fallback_on_all_providers_failed(monkeypatch):
    """Graceful fallback to keyword rules when LLM providers fail."""
    monkeypatch.setattr("taskflow.adapters.llm.router.provider_priority", lambda: ["claude"])
    monkeypatch.setattr("taskflow.adapters.llm.router.model_for", lambda p, pur: "test-model")

    router = ProviderRouter(providers={"claude": MockFailingClassifierProvider()})
    intent, confidence, _call = await classify_intent(
        "I need an invoice for my company", router=router
    )

    assert intent == Intent.BILLING
    assert confidence == 0.90


@pytest.mark.asyncio
async def test_empty_input_handling():
    """Empty string returns Intent.UNKNOWN with 0.0 confidence."""
    intent, confidence, _call = await classify_intent("   ", router=None)
    assert intent == Intent.UNKNOWN
    assert confidence == 0.0
