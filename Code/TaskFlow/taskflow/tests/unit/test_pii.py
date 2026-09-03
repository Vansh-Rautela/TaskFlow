"""PII detection: the regex detector and the injection seam.

No live model here — `RegexPIIDetector` is pure Python, and the stub detector below
proves `preprocess_message_text`/`validate_pii` actually use whatever `PIIDetector`
they're given rather than silently falling back to the default. The Presidio-backed
detector itself is exercised in `tests/integration/test_pii_detection.py`, which
loads a real spaCy model and is excluded from `make test` for exactly that reason.
"""

import pytest

from taskflow.domain.models import DraftOutput
from taskflow.services.pii import RegexPIIDetector, first_match, redact
from taskflow.services.preprocess.service import preprocess_message_text
from taskflow.services.validate.pii_leak import validate_pii


class _StubDetector:
    """A `PIIDetector` that redacts nothing `RegexPIIDetector` would — proves the
    caller used *this* detector, not the module-level default."""

    def redact(self, text: str) -> str:
        return text.replace("Ashwin", "[REDACTED_PII]")

    def first_match(self, text: str) -> str | None:
        return "Person Name" if "Ashwin" in text else None


def test_regex_pii_detector_matches_the_free_functions():
    """RegexPIIDetector.redact/first_match must agree with services.pii's module-level
    functions — it exists to wrap them for dependency injection, not to reimplement."""
    detector = RegexPIIDetector()
    text = "Card 4111 2222 3333 4444, SSN 123-45-6789, key sk_notarealkey_0000000000000000."

    assert detector.redact(text) == redact(text)
    assert detector.first_match(text) == first_match(text)


def test_regex_pii_detector_passes_clean_text_through():
    detector = RegexPIIDetector()
    assert detector.redact("Thanks for reaching out.") == "Thanks for reaching out."
    assert detector.first_match("Thanks for reaching out.") is None


def test_preprocess_uses_the_default_detector_when_none_is_injected():
    text = "Card 4111 2222 3333 4444."
    assert preprocess_message_text(text) == "Card [REDACTED_CREDIT_CARD]."


def test_preprocess_uses_an_injected_detector_over_the_default():
    """The regex default would not touch a bare name — the stub proves injection
    actually took effect rather than the default silently running anyway."""
    text = "Hi, this is Ashwin following up on my invoice."
    assert preprocess_message_text(text) == text  # default: no fixed-format PII here

    redacted = preprocess_message_text(text, _StubDetector())
    assert redacted == "Hi, this is [REDACTED_PII] following up on my invoice."


@pytest.mark.asyncio
async def test_validate_pii_uses_an_injected_detector_over_the_default():
    draft = DraftOutput(
        response_text="Thanks, Ashwin — we'll take care of it.",
        citations=[],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )

    default_result = await validate_pii(draft)
    assert default_result.passed  # regex default: nothing fixed-format to catch

    injected_result = await validate_pii(draft, _StubDetector())
    assert not injected_result.passed
    assert "Person Name" in injected_result.reason
