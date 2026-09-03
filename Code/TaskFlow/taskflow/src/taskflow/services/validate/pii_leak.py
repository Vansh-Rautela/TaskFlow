"""PII and secret leakage validator.

Scans an outbound draft for the same things `preprocess` strips on the way in.
A hit is blocking: gate G2 vetoes auto-send.
"""

from taskflow.domain.models import DraftOutput, ValidatorResult
from taskflow.ports.pii import PIIDetector
from taskflow.services.pii import RegexPIIDetector

_default_detector: PIIDetector = RegexPIIDetector()


async def validate_pii(draft: DraftOutput, detector: PIIDetector | None = None) -> ValidatorResult:
    """Fail the draft if it contains sensitive PII or API credentials.

    `detector` defaults to the dependency-free regex detector, matching
    `preprocess.service.preprocess_message_text` — see its docstring for why.
    """
    leaked = (detector or _default_detector).first_match(draft.response_text or "")

    if leaked:
        return ValidatorResult(
            validator_name="pii_leak",
            passed=False,
            score=0.0,
            reason=f"Detected potential PII leak ({leaked} format)",
            blocking=True,
        )

    return ValidatorResult(
        validator_name="pii_leak", passed=True, score=1.0, reason="ok", blocking=False
    )
