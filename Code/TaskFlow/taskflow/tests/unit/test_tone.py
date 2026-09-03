"""Tone alignment signal.

Before `services/confidence/tone.py` existed the orchestrator passed a constant 1.0
for this signal, so 10% of every routing decision came from a measurement nobody was
taking. These tests exist so it cannot quietly become a constant again.
"""

import pytest

from taskflow.domain.enums import Intent, Severity
from taskflow.domain.models import DraftOutput, PolicyViolation
from taskflow.services.confidence.tone import score_tone


def draft(tone: str = "neutral", text: str = "Here is the answer.") -> DraftOutput:
    return DraftOutput(response_text=text, tone=tone, complexity="simple")


def violation(severity: Severity, rule_id: str = "r") -> PolicyViolation:
    return PolicyViolation(rule_id=rule_id, severity=severity, description="d", matched_text="m")


def test_clean_draft_scores_one():
    score, reason = score_tone(draft(), Intent.BILLING, [])
    assert score == 1.0
    assert reason == "tone aligned"


def test_warning_violations_reduce_the_score():
    """Severity.WARNING is documented as "reduces tone score, does not block"."""
    one, _ = score_tone(draft(), Intent.BILLING, [violation(Severity.WARNING)])
    two, _ = score_tone(
        draft(),
        Intent.BILLING,
        [violation(Severity.WARNING, "a"), violation(Severity.WARNING, "b")],
    )
    assert one == pytest.approx(0.75)
    assert two == pytest.approx(0.50)
    assert two < one < 1.0


def test_critical_violations_do_not_touch_the_tone_score():
    """A critical violation is a gate veto, not a weighted deduction. Letting it also
    reduce the score would double-count it."""
    score, _ = score_tone(draft(), Intent.BILLING, [violation(Severity.CRITICAL)])
    assert score == 1.0


@pytest.mark.parametrize("intent", [Intent.COMPLAINT, Intent.REFUND])
def test_upset_intent_needs_an_acknowledging_tone(intent):
    neutral, reason = score_tone(draft(tone="neutral"), intent, [])
    apologetic, _ = score_tone(draft(tone="apologetic"), intent, [])
    friendly, _ = score_tone(draft(tone="friendly"), intent, [])

    assert neutral == pytest.approx(0.70)
    assert "not acknowledging" in reason
    assert apologetic == 1.0
    assert friendly == 1.0


def test_calm_intent_accepts_a_neutral_tone():
    assert score_tone(draft(tone="neutral"), Intent.TECHNICAL, [])[0] == 1.0


def test_score_is_clamped_to_the_unit_interval():
    many = [violation(Severity.WARNING, f"r{i}") for i in range(10)]
    score, _ = score_tone(draft(tone="neutral"), Intent.COMPLAINT, many)
    assert score == 0.0


def test_missing_draft_scores_zero():
    score, reason = score_tone(None, Intent.BILLING, [])
    assert score == 0.0
    assert "no draft" in reason


def test_the_signal_is_not_a_constant():
    """The regression this module exists for."""
    outcomes = {
        score_tone(draft(tone="neutral"), Intent.BILLING, [])[0],
        score_tone(draft(tone="neutral"), Intent.COMPLAINT, [])[0],
        score_tone(draft(tone="neutral"), Intent.COMPLAINT, [violation(Severity.WARNING)])[0],
    }
    assert len(outcomes) == 3, f"tone_alignment produced only {outcomes}"
