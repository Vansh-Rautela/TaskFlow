"""Presidio-based PII detector.

`services.pii.RegexPIIDetector` only recognizes fixed-format text — a credit card
number, an SSN, an API key — because a regex cannot know a string is a person's
name or that ten digits are a phone number rather than an account ID. This adapter
adds Microsoft Presidio's `AnalyzerEngine`, running a local spaCy model, to cover
those free-form entities: PERSON, EMAIL_ADDRESS, PHONE_NUMBER.

The three fixed-format entities (CREDIT_CARD, US_SSN, API_KEY) are *not* left to
Presidio's own built-in recognizers. Its stock credit-card pattern is explicitly
named "(weak)" and scores only 0.3 — below any `score_threshold` high enough to
keep PERSON's false-positive rate down — so a plain "4111 2222 3333 4444" was
silently passing through uncaught. Instead this registers `services.pii`'s own
patterns, the same ones `RegexPIIDetector` uses, at a high fixed score, so these
three formats behave at least as well here as they already did without Presidio.

Implements `ports.pii.PIIDetector`. Model name and score threshold come from
`config/settings.yaml`'s `pii` block (ADR-004, configuration over code) — never hardcoded here.
"""

from typing import cast

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from presidio_anonymizer.entities.engine.recognizer_result import (
    RecognizerResult as AnonymizerRecognizerResult,
)

from taskflow.config.settings import pii_config
from taskflow.services.pii import PII_PATTERNS

_ENTITIES = ["CREDIT_CARD", "US_SSN", "PHONE_NUMBER", "EMAIL_ADDRESS", "PERSON", "API_KEY"]

# The three fixed-format labels stay identical to RegexPIIDetector's, so a trace or
# an alert reads the same reason regardless of which detector caught it. The three
# free-form entities Presidio adds get their own labels.
_LABEL_BY_ENTITY: dict[str, str] = {
    "CREDIT_CARD": "Credit Card",
    "US_SSN": "Social Security Number",
    "API_KEY": "API secret or Bearer token",
    "PERSON": "Person Name",
    "EMAIL_ADDRESS": "Email Address",
    "PHONE_NUMBER": "Phone Number",
}

_REPLACEMENT_BY_ENTITY: dict[str, str] = {
    "CREDIT_CARD": "[REDACTED_CREDIT_CARD]",
    "US_SSN": "[REDACTED_SSN]",
    "API_KEY": "[REDACTED_API_KEY]",
    "PERSON": "[REDACTED_PII]",
    "EMAIL_ADDRESS": "[REDACTED_PII]",
    "PHONE_NUMBER": "[REDACTED_PII]",
}


# services.pii label -> the Presidio entity it should register as.
_FIXED_FORMAT_ENTITY_BY_LABEL = {
    "Credit Card": "CREDIT_CARD",
    "Social Security Number": "US_SSN",
    "API secret or Bearer token": "API_KEY",
}


def _fixed_format_recognizers() -> list[PatternRecognizer]:
    """Presidio's own CREDIT_CARD recognizer is explicitly named "(weak)" and
    scores 0.3 — below any score_threshold that also has to keep PERSON's false-
    positive rate down, so "4111 2222 3333 4444" was silently passing through.

    Registering `services.pii`'s own patterns — the same ones `RegexPIIDetector`
    uses, so behaviour cannot regress — at a high, fixed score fixes that: these
    three formats are either present or not, so there is no ambiguity a lower
    score should be hedging against the way there is for a named entity."""
    recognizers = []
    for label, compiled, _replacement in PII_PATTERNS:
        entity = _FIXED_FORMAT_ENTITY_BY_LABEL[label]
        recognizers.append(
            PatternRecognizer(
                supported_entity=entity,
                patterns=[
                    Pattern(name=f"taskflow_{entity.lower()}", regex=compiled.pattern, score=0.9)
                ],
            )
        )
    return recognizers


class PresidioDetector:
    """Satisfies `ports.pii.PIIDetector`. Loads a real spaCy model at construction
    time — expensive enough that this belongs in `bootstrap.py`'s production
    wiring, not in a unit-test fixture. See `tests/integration/test_pii_detection.py`
    for the tests that actually load it."""

    def __init__(self) -> None:
        cfg = pii_config()
        model_name = str(cfg.get("spacy_model", "en_core_web_sm"))
        self._score_threshold = float(cfg.get("score_threshold", 0.4))
        self._allow_list = [str(term) for term in cfg.get("allow_list", [])]

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": model_name}],
            }
        )
        self._analyzer = AnalyzerEngine(nlp_engine=provider.create_engine())
        for recognizer in _fixed_format_recognizers():
            self._analyzer.registry.add_recognizer(recognizer)
        self._anonymizer = AnonymizerEngine()

    def _analyze(self, text: str) -> list[RecognizerResult]:
        if not text:
            return []
        return self._analyzer.analyze(
            text=text,
            language="en",
            entities=_ENTITIES,
            score_threshold=self._score_threshold,
            allow_list=self._allow_list,
            allow_list_match="exact",
        )

    def redact(self, text: str) -> str:
        if not text:
            return ""
        results = self._analyze(text)
        if not results:
            return text
        operators = {
            entity: OperatorConfig("replace", {"new_value": replacement})
            for entity, replacement in _REPLACEMENT_BY_ENTITY.items()
        }
        # presidio-analyzer and presidio-anonymizer each define their own
        # RecognizerResult class — structurally identical (entity_type/start/end/
        # score), no inheritance relationship between the two packages. Passing one
        # where the other is declared is the documented usage and works at runtime;
        # only the nominal types disagree.
        return self._anonymizer.anonymize(
            text=text,
            analyzer_results=cast(list[AnonymizerRecognizerResult], results),
            operators=operators,
        ).text

    def first_match(self, text: str) -> str | None:
        results = self._analyze(text)
        if not results:
            return None
        earliest = min(results, key=lambda r: r.start)
        return _LABEL_BY_ENTITY.get(earliest.entity_type, earliest.entity_type)
