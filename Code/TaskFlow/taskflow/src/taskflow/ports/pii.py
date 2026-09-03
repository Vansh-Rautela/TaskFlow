"""PII detection port."""

from typing import Protocol


class PIIDetector(Protocol):
    """Services depend on this; only adapters/pii and services.pii.RegexPIIDetector
    implement it.

    Two implementations exist on purpose. `services.pii.RegexPIIDetector` is pure
    Python — no vendor SDK, no model to load — so it is what every entrypoint gets
    by default and what tests run against without a live model. `adapters.pii.
    PresidioDetector` adds named-entity recognition (a name, an email, a phone
    number — nothing a fixed-format regex can recognize) and is what `bootstrap.py`
    wires into production traffic.
    """

    def redact(self, text: str) -> str:
        """Return `text` with every recognized PII/secret span replaced by a
        placeholder. Returns `""` for empty input, the input unchanged if nothing
        was found."""
        ...

    def first_match(self, text: str) -> str | None:
        """Return a human-readable label for the first PII/secret span found in
        `text`, or `None` if nothing was found."""
        ...
