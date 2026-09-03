"""The PII and secret patterns, defined once.

Two stages need the same definition of "sensitive": `preprocess` redacts these out
of an inbound message before it reaches a provider, and `validate.pii_leak` fails a
draft that puts them back in. When the two lists were maintained separately, a
pattern added to one silently left the other blind to it.

Pure Python, no vendor SDK — this is what makes `RegexPIIDetector` usable directly
from `services/`, which `adapters.pii.PresidioDetector` (named-entity recognition on
top of these fixed formats) cannot be: the layering rule keeps a vendor SDK out of
this layer, full stop. See `ports.pii.PIIDetector` for how the two relate.
"""

import re

# (label, pattern, replacement) — order is the order they are applied and reported in.
PII_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("Credit Card", re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CREDIT_CARD]"),
    ("Social Security Number", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (
        "API secret or Bearer token",
        re.compile(
            r"\b(?:sk|pk)_[a-zA-Z0-9_\-]{16,}\b|Bearer\s+[a-zA-Z0-9._\-]{20,}", re.IGNORECASE
        ),
        "[REDACTED_API_KEY]",
    ),
)


def redact(text: str) -> str:
    """Replace every known PII pattern with its placeholder."""
    if not text:
        return ""
    for _label, pattern, replacement in PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def first_match(text: str) -> str | None:
    """Return the label of the first PII pattern found in `text`, or None."""
    for label, pattern, _replacement in PII_PATTERNS:
        if pattern.search(text):
            return label
    return None


class RegexPIIDetector:
    """Satisfies `ports.pii.PIIDetector` by wrapping the two functions above.

    The default detector everywhere: `preprocess.service` and `validate.pii_leak`
    fall back to an instance of this when no detector is injected, so every existing
    caller and test keeps working — unchanged, model-free, sub-millisecond — and
    only `bootstrap.py`'s production wiring has to know `PresidioDetector` exists.
    """

    def redact(self, text: str) -> str:
        return redact(text)

    def first_match(self, text: str) -> str | None:
        return first_match(text)
