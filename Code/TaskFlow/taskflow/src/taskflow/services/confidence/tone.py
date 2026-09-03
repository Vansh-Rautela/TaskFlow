"""Tone alignment signal.

`Severity.WARNING` is defined in the domain as "logged, reduces tone score, does not
block". This module is that reduction. Before it existed the orchestrator passed a
constant 1.0, so a tenth of every routing decision rested on a measurement nobody
was taking.

Deterministic and config-driven: no LLM call, no hidden constants.
"""

from taskflow.config.settings import thresholds_config
from taskflow.domain.enums import Intent, Severity
from taskflow.domain.models import DraftOutput, PolicyViolation

# Intents where a customer is upset and a bare `neutral` reply reads as dismissive.
_APOLOGETIC_INTENTS = frozenset({Intent.COMPLAINT, Intent.REFUND})
_ACCEPTABLE_FOR_UPSET = frozenset({"apologetic", "friendly"})


def score_tone(
    draft: DraftOutput | None,
    intent: Intent,
    violations: list[PolicyViolation],
) -> tuple[float, str]:
    """Return (score in 0.0-1.0, human-readable reason).

    Starts at 1.0 and subtracts configured penalties for:
      - each warning-severity policy violation (e.g. missing acknowledgement
        language, competitor disparagement);
      - a tone that does not match an intent where the customer is upset.
    """
    cfg = thresholds_config().get("tone", {})
    per_warning = float(cfg.get("warning_penalty", 0.25))
    mismatch_penalty = float(cfg.get("intent_mismatch_penalty", 0.30))

    if draft is None:
        return 0.0, "no draft to assess"

    score = 1.0
    reasons: list[str] = []

    warnings = [v for v in violations if v.severity == Severity.WARNING]
    if warnings:
        score -= per_warning * len(warnings)
        reasons.append(f"{len(warnings)} warning-severity policy violation(s)")

    if intent in _APOLOGETIC_INTENTS and draft.tone not in _ACCEPTABLE_FOR_UPSET:
        score -= mismatch_penalty
        reasons.append(f"tone '{draft.tone}' is not acknowledging for intent '{intent.value}'")

    score = max(0.0, min(1.0, score))
    return score, "; ".join(reasons) if reasons else "tone aligned"
