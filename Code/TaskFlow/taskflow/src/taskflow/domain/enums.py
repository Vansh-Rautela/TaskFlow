from enum import StrEnum


class Channel(StrEnum):
    EMAIL = "email"
    WEBCHAT = "webchat"
    CONSOLE = "console"


class Intent(StrEnum):
    BILLING = "billing"
    TECHNICAL = "technical"
    ACCOUNT = "account"
    FEATURE_REQUEST = "feature_request"
    REFUND = "refund"
    COMPLAINT = "complaint"
    GENERAL = "general"
    UNKNOWN = "unknown"  # abstain target — never auto-sends


PREEMPT_INTENTS = frozenset({Intent.COMPLAINT, Intent.UNKNOWN})


class RouteAction(StrEnum):
    AUTO_SEND = "auto_send"
    HUMAN_REVIEW = "human_review"
    TEMPLATE_SENT = "template_sent"
    REJECTED_SPAM = "rejected_spam"
    DROPPED_DUPLICATE = "dropped_duplicate"


class GateId(StrEnum):
    G1_POLICY = "G1_policy_critical"
    G2_PII = "G2_pii_leak"
    G3_CITATIONS = "G3_citations_resolve"
    G4_INTENT = "G4_intent_allows_auto"
    G5_ABSTAIN = "G5_classifier_confident"
    G6_VALIDATORS = "G6_validators_healthy"
    G7_INJECTION = "G7_no_suspicious_context"
    G8_DRAFT_SOURCE = "G8_draft_model_generated"


class ReviewState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class OutboxState(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"


class Severity(StrEnum):
    CRITICAL = "critical"  # blocks auto-send via G1
    WARNING = "warning"  # logged, reduces tone score, does not block
