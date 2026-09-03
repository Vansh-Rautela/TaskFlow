"""Policy Evaluation Service for TaskFlow.

Evaluates inbound customer messages and outbound LLM drafts against the rules
configured in config/policies.yaml, returning a list of PolicyViolations.
"""

import re
from typing import Any

from taskflow.config.settings import policies_config
from taskflow.domain.enums import Intent, Severity
from taskflow.domain.models import DraftOutput, InboundMessage, PolicyViolation


def evaluate_policies(
    msg: InboundMessage,
    draft: DraftOutput | None,
    intent: Intent,
) -> list[PolicyViolation]:
    """Evaluate inbound message and outbound response draft against policies.yaml rules."""
    cfg = policies_config()
    rules: list[dict[str, Any]] = cfg.get("rules", [])
    violations: list[PolicyViolation] = []

    inbound_text = (msg.body_text or "").lower()
    draft_text = (draft.response_text or "") if draft else ""
    draft_text_lower = draft_text.lower()

    for rule in rules:
        rule_id = str(rule.get("id", "unknown"))
        severity_str = str(rule.get("severity", "warning")).lower()
        severity = Severity.CRITICAL if severity_str == "critical" else Severity.WARNING
        description = str(rule.get("description", rule_id))
        applies_to = rule.get("applies_to", "outbound")
        applies_when = rule.get("applies_when", {})
        detect = rule.get("detect", {})

        # Filter by intent if specified in applies_when
        target_intents = applies_when.get("intent", [])
        if target_intents and intent.value not in target_intents and intent not in target_intents:
            continue

        detect_type = detect.get("type", "")

        target_text = inbound_text if applies_to == "inbound" else draft_text_lower

        if detect_type == "amount_over":
            pattern = detect.get("pattern", r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")
            threshold = float(detect.get("threshold", 500))
            for match in re.finditer(pattern, draft_text):
                try:
                    amount_str = match.group(1).replace(",", "")
                    amount = float(amount_str)
                    if amount > threshold:
                        violations.append(
                            PolicyViolation(
                                rule_id=rule_id,
                                severity=severity,
                                description=description,
                                matched_text=match.group(0),
                            )
                        )
                except ValueError:
                    continue

        elif detect_type == "regex_any":
            for pat in detect.get("patterns", []):
                found = re.search(pat, target_text, re.IGNORECASE)
                if found:
                    violations.append(
                        PolicyViolation(
                            rule_id=rule_id,
                            severity=severity,
                            description=description,
                            matched_text=found.group(0),
                        )
                    )
                    break  # one report per rule

        elif detect_type == "regex_absent":
            patterns = detect.get("patterns", [])
            if draft_text and not any(
                re.search(pat, draft_text, re.IGNORECASE) for pat in patterns
            ):
                violations.append(
                    PolicyViolation(
                        rule_id=rule_id,
                        severity=severity,
                        description=description,
                        matched_text="[Absence of required acknowledgment language]",
                    )
                )

        elif detect_type == "price_mismatch":
            violations.extend(
                _detect_price_mismatch(rule_id, severity, description, detect, draft_text)
            )

    return violations


# A price is only "mismatched" if it is quoted near the plan it belongs to. Scanning
# the whole draft for every plan named anywhere in it flagged unrelated figures — and
# a bare `plan in text` test matched "pro" inside "provide", "process", "problem".
_PLAN_PRICE_WINDOW = 80


def _detect_price_mismatch(
    rule_id: str,
    severity: Severity,
    description: str,
    detect: dict[str, Any],
    draft_text: str,
) -> list[PolicyViolation]:
    """Flag a price quoted within `_PLAN_PRICE_WINDOW` characters of a plan name."""
    known: dict[str, int] = detect.get("known", {})
    violations: list[PolicyViolation] = []

    for plan, expected_price in known.items():
        # \b so "pro" does not match "provide"; the plan name must stand alone.
        for plan_match in re.finditer(rf"\b{re.escape(plan)}\b", draft_text, re.IGNORECASE):
            start = max(0, plan_match.start() - _PLAN_PRICE_WINDOW)
            window = draft_text[start : plan_match.end() + _PLAN_PRICE_WINDOW]

            for price_match in re.finditer(r"\$\s?(\d+)", window):
                quoted = int(price_match.group(1))
                if quoted > 0 and quoted != int(expected_price):
                    violations.append(
                        PolicyViolation(
                            rule_id=rule_id,
                            severity=severity,
                            description=description,
                            matched_text=f"{plan}: ${quoted} (expected ${expected_price})",
                        )
                    )
            break  # one report per plan is enough

    return violations
