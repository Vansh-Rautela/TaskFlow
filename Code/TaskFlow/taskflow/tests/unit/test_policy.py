"""Unit tests for Policy Evaluation Service."""

from datetime import UTC, datetime

from taskflow.domain.enums import Channel, Intent, Severity
from taskflow.domain.models import DraftOutput, InboundMessage
from taskflow.services.policy.service import evaluate_policies


def make_msg(body: str) -> InboundMessage:
    return InboundMessage(
        message_id="msg-test-policy",
        dedupe_key="console:msg-test-policy",
        tenant_id="taskflow-demo",
        channel=Channel.CONSOLE,
        sender="user@example.com",
        body_text=body,
        body_redacted=body,
        provider_message_id="msg-test-policy",
        received_at=datetime.now(UTC),
    )


def test_refund_ceiling_violation():
    msg = make_msg("I want a refund for my subscription.")
    draft = DraftOutput(
        response_text="We have processed a full refund of $650.00 to your card.",
        citations=[],
        tone="formal",
        complexity="simple",
        draft_confidence=0.9,
    )
    violations = evaluate_policies(msg, draft, Intent.REFUND)
    assert any(v.rule_id == "refund_ceiling" and v.severity == Severity.CRITICAL for v in violations)


def test_legal_threat_inbound_violation():
    msg = make_msg("If you do not fix this immediately, I am filing a lawsuit and taking legal action!")
    draft = DraftOutput(
        response_text="Thank you for reaching out. We are looking into your issue.",
        citations=[],
        tone="formal",
        complexity="simple",
        draft_confidence=0.9,
    )
    violations = evaluate_policies(msg, draft, Intent.COMPLAINT)
    assert any(v.rule_id == "legal_threat_escalation" and v.severity == Severity.CRITICAL for v in violations)


def test_no_credential_request_violation():
    msg = make_msg("I cannot log into my account.")
    draft = DraftOutput(
        response_text="Please send me your password and 2fa code so I can reset it for you.",
        citations=[],
        tone="formal",
        complexity="simple",
        draft_confidence=0.9,
    )
    violations = evaluate_policies(msg, draft, Intent.ACCOUNT)
    assert any(v.rule_id == "no_credential_request" and v.severity == Severity.CRITICAL for v in violations)


def test_gdpr_deletion_routing():
    msg = make_msg("Please delete all my personal data and erase my account immediately.")
    draft = DraftOutput(
        response_text="We will submit your deletion request to our privacy team.",
        citations=[],
        tone="formal",
        complexity="simple",
        draft_confidence=0.9,
    )
    violations = evaluate_policies(msg, draft, Intent.GENERAL)
    assert any(v.rule_id == "gdpr_deletion_routing" and v.severity == Severity.CRITICAL for v in violations)


def test_clean_draft_no_violations():
    msg = make_msg("How do I change my billing email address?")
    draft = DraftOutput(
        response_text="You can change your billing email under Account Settings -> Billing.",
        citations=[],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.95,
    )
    violations = evaluate_policies(msg, draft, Intent.BILLING)
    critical_violations = [v for v in violations if v.severity == Severity.CRITICAL]
    assert len(critical_violations) == 0
