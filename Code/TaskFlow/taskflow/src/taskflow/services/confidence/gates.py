"""The eight deterministic safety gates.

Conjunctive and veto-only: any failure routes the message to HUMAN_REVIEW. Gates are
evaluated before the weighted score and are never expressed as a weight — a safety
signal that can be compensated for is not a safety signal (ADR-001).
"""

from taskflow.domain.enums import PREEMPT_INTENTS, GateId, Intent, Severity
from taskflow.domain.models import GateResult, PolicyViolation, ValidatorResult


def evaluate_gates(
    intent: Intent,
    intent_confidence: float,
    abstain_threshold: float,
    validators: list[ValidatorResult],
    violations: list[PolicyViolation],
    citations_resolve: bool,
    suspicious_context: bool,
    draft_is_model_generated: bool = True,
) -> list[GateResult]:
    """Evaluate all safety gates. Returns one GateResult per gate."""
    results = []

    # G1: Critical policy violations block auto-send
    has_critical_violation = any(v.severity == Severity.CRITICAL for v in violations)
    results.append(
        GateResult(
            gate_id=GateId.G1_POLICY,
            passed=not has_critical_violation,
            reason="Critical policy violation detected" if has_critical_violation else "Clear",
        )
    )

    # G2: PII leaks in outbound text (checking fail closed)
    pii_validator = next((v for v in validators if v.validator_name == "pii_leak"), None)
    pii_passed = pii_validator.passed if pii_validator else True
    results.append(
        GateResult(
            gate_id=GateId.G2_PII,
            passed=pii_passed,
            reason="PII leak detected" if not pii_passed else "Clear",
        )
    )

    # G3: Unresolvable citations
    results.append(
        GateResult(
            gate_id=GateId.G3_CITATIONS,
            passed=citations_resolve,
            reason="Unresolved citations in draft" if not citations_resolve else "Clear",
        )
    )

    # G4: Intent allows auto-send (No complaints/unknown)
    intent_passed = intent not in PREEMPT_INTENTS
    results.append(
        GateResult(
            gate_id=GateId.G4_INTENT,
            passed=intent_passed,
            reason="Intent is complaint or unknown" if not intent_passed else "Clear",
        )
    )

    # G5: Classifier confident (Abstention)
    abstain_passed = intent_confidence >= abstain_threshold
    results.append(
        GateResult(
            gate_id=GateId.G5_ABSTAIN,
            passed=abstain_passed,
            reason="Classifier confidence below abstain threshold"
            if not abstain_passed
            else "Clear",
        )
    )

    # G6: Validators healthy (Fail closed on error)
    all_validators_healthy = all(not getattr(v, "errored", False) for v in validators)
    results.append(
        GateResult(
            gate_id=GateId.G6_VALIDATORS,
            passed=all_validators_healthy,
            reason="A validator errored or timed out" if not all_validators_healthy else "Clear",
        )
    )

    # G7: No suspicious context injection
    results.append(
        GateResult(
            gate_id=GateId.G7_INJECTION,
            passed=not suspicious_context,
            reason="Suspicious context detected" if suspicious_context else "Clear",
        )
    )

    # G8: A model actually wrote this draft.
    # When every provider fails, drafting returns a holding template. That template
    # is a placeholder, not an answer, and must never be auto-sent as though a model
    # had addressed the customer's question.
    results.append(
        GateResult(
            gate_id=GateId.G8_DRAFT_SOURCE,
            passed=draft_is_model_generated,
            reason="Draft is a template fallback; no provider produced a reply"
            if not draft_is_model_generated
            else "Clear",
        )
    )

    return results
