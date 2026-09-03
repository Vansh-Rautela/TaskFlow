"""PII detection against the real Presidio + spaCy pipeline.

`services.pii.RegexPIIDetector` only recognizes fixed-format text. These tests
assert the property that makes `PresidioDetector` worth the extra dependency: it
also catches a person's name, an email address, and a phone number — none of which
have a "format" a regex could pin down.

Marked `integration`: loads a real spaCy model and is excluded from `make test`.
"""

import pytest

from taskflow.adapters.pii.presidio_detector import PresidioDetector
from taskflow.domain.models import DraftOutput
from taskflow.services.validate.pii_leak import validate_pii

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def detector() -> PresidioDetector:
    return PresidioDetector()


def test_person_name_is_redacted(detector: PresidioDetector):
    text = "Hi, my name is John Carter and I need help with my invoice."
    assert "John Carter" not in detector.redact(text)


def test_email_address_is_redacted(detector: PresidioDetector):
    text = "You can reach me at john.carter@example.com if needed."
    assert "john.carter@example.com" not in detector.redact(text)


def test_phone_number_is_redacted(detector: PresidioDetector):
    text = "Call me back at 415-555-0132 this afternoon."
    assert "415-555-0132" not in detector.redact(text)


def test_clean_text_is_returned_unchanged(detector: PresidioDetector):
    text = "What payment methods do you support for paying my invoice?"
    assert detector.redact(text) == text
    assert detector.first_match(text) is None


def test_fixed_format_entities_still_work(detector: PresidioDetector):
    """PresidioDetector must not regress what RegexPIIDetector already caught —
    credit cards, SSNs, and the project's own API-key/Bearer-token pattern."""
    assert detector.redact("Card 4111 2222 3333 4444") == "Card [REDACTED_CREDIT_CARD]"
    assert detector.redact("SSN 123-45-6789") == "SSN [REDACTED_SSN]"
    redacted = detector.redact("Use key sk_notarealkey_0000000000000000 for auth")
    assert "[REDACTED_API_KEY]" in redacted
    assert "sk_notarealkey_" not in redacted


def test_first_match_labels_a_name(detector: PresidioDetector):
    assert detector.first_match("This is Jane Doe writing in.") == "Person Name"


@pytest.mark.asyncio
async def test_gate_g2_catches_a_leaked_name_that_regex_would_miss(detector: PresidioDetector):
    """The end-to-end point of this adapter: a draft that leaks a customer's name
    fails the pii_leak validator (which fails gate G2) when Presidio is injected,
    and would not have with the regex default — see tests/unit/test_pii.py."""
    draft = DraftOutput(
        response_text="Thanks for reaching out, Jane Doe — we'll process that today.",
        citations=[],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    result = await validate_pii(draft, detector)
    assert not result.passed
    assert result.blocking
