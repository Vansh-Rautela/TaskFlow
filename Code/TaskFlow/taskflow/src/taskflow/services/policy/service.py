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

        # Target text selection based on applies_to
        if applies_to == "inbound":
            target_text = inbound_text
        else:
            target_text = draft_text_lower

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
            patterns = detect.get("patterns", [])
            for pat in patterns:
                match = re.search(pat, target_text, re.IGNORECASE)
                if match:
                    violations.append(
                        PolicyViolation(
                            rule_id=rule_id,
                            severity=severity,
                            description=description,
                            matched_text=match.group(0),
                        )
                    )
                    break

        elif detect_type == "regex_absent":
            patterns = detect.get("patterns", [])
            if draft_text and not any(re.search(pat, draft_text, re.IGNORECASE) for pat in patterns):
                violations.append(
                    PolicyViolation(
                        rule_id=rule_id,
                        severity=severity,
                        description=description,
                        matched_text="[Absence of required acknowledgment language]",
                    )
                )

        elif detect_type == "price_mismatch":
            known_prices = detect.get("known", {})
            for plan, expected_price in known_prices.items():
                if plan in draft_text_lower:
                    for match in re.finditer(r"\$\s?(\d+)", draft_text):
                        quoted = int(match.group(1))
                        if quoted != expected_price and quoted > 0:
                            violations.append(
                                PolicyViolation(
                                    rule_id=rule_id,
                                    severity=severity,
                                    description=description,
                                    matched_text=f"{plan}: ${quoted} (expected ${expected_price})",
                                )
                            )

    return violations
