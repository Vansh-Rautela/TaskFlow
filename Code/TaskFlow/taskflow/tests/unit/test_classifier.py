"""Unit tests for Phase P6 Intent Classification."""

import pytest
from pydantic import BaseModel

from taskflow.adapters.llm.router import ProviderRouter
from taskflow.config.settings import thresholds_config
from taskflow.domain.enums import Intent
from taskflow.domain.errors import ProviderError
from taskflow.domain.models import ClassificationOutput, LLMCall
from taskflow.ports.llm import LLMResponse, ProviderCapabilities
from taskflow.services.classify.service import build_system_prompt, classify_intent


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
    intent, confidence, call = await classify_intent("Can I get a refund?", router=None)
    assert intent == Intent.REFUND
    assert confidence == thresholds_config()["fallback_confidences"]["refund"]
    assert call is None, "the keyword path makes no LLM call and must not report one"


@pytest.mark.asyncio
async def test_cancellation_routes_to_account_not_refund():
    """Regression for BRD R-2/§15.

    There is no `cancellation` intent in the 8-intent taxonomy. Asking to close an
    account is not asking for money back, and the keyword classifier used to answer
    every cancellation with REFUND.
    """
    for text in (
        "Please cancel my subscription",
        "I want to close my account",
        "Please erase my data and close account",
    ):
        intent, _conf, _call = await classify_intent(text, router=None)
        assert intent == Intent.ACCOUNT, f"{text!r} classified as {intent}"


@pytest.mark.asyncio
async def test_prompt_enumerates_exactly_the_intent_enum():
    """BRD FR-3.6. The prompt used to list `cancellation` and `enterprise`, which are
    not `Intent` members, so those labels failed validation and silently degraded to
    the keyword classifier."""
    prompt = build_system_prompt()
    for intent in Intent:
        assert f"- {intent.value}:" in prompt, f"{intent.value} missing from the prompt"

    listed = {
        line.split(":")[0].removeprefix("- ").strip()
        for line in prompt.splitlines()
        if line.startswith("- ")
    }
    assert listed == {i.value for i in Intent}, "prompt and Intent enum have drifted apart"


@pytest.mark.asyncio
async def test_fallback_confidence_never_exceeds_a_confident_llm():
    """A keyword match is weak evidence. It must not be stamped with a confidence
    that sails past the G5 abstain gate as though the classifier were sure."""
    confidences = thresholds_config()["fallback_confidences"]
    for key, value in confidences.items():
        if key == "draft":
            continue
        assert value <= 0.80, f"fallback confidence {key}={value} is overconfident"


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
    assert confidence == thresholds_config()["fallback_confidences"]["billing"]


@pytest.mark.asyncio
async def test_empty_input_handling():
    """Empty string returns Intent.UNKNOWN with 0.0 confidence."""
    intent, confidence, _call = await classify_intent("   ", router=None)
    assert intent == Intent.UNKNOWN
    assert confidence == 0.0
