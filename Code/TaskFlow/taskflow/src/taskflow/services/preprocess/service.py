"""Inbound message preprocessing.

Redacts sensitive PII from inbound message text before it is passed to an LLM, so
raw credentials — and, with `PresidioDetector` injected, a name, email, or phone
number — never enter prompt context. `services.pii` is shared with the outbound
validator so both stages agree on what "sensitive" means.
"""

from taskflow.ports.pii import PIIDetector
from taskflow.services.pii import RegexPIIDetector

_default_detector: PIIDetector = RegexPIIDetector()


def preprocess_message_text(text: str, detector: PIIDetector | None = None) -> str:
    """Return PII-redacted text safe for LLM prompt context.

    `detector` defaults to the dependency-free regex detector so every existing
    caller keeps working unchanged; `bootstrap.py` injects `PresidioDetector` for
    real traffic.
    """
    return (detector or _default_detector).redact(text)
