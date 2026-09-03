"""Intent classification.

Primary path: LLM structured output conforming to ``ClassificationOutput``.
Fallback path: a deterministic keyword classifier used when no router is supplied
or when every provider fails.

The prompt's intent list is generated from the ``Intent`` enum so the two can
never drift apart (BRD FR-3.6 / R-2).
"""

import structlog

from taskflow.config.settings import thresholds_config
from taskflow.domain.enums import Intent
from taskflow.domain.errors import AllProvidersFailed, ProviderError, TransientError
from taskflow.domain.models import ClassificationOutput, LLMCall
from taskflow.ports.llm import LLMRouter

logger = structlog.get_logger()

# One line of guidance per member of the Intent enum. Every enum member must appear
# here; the test suite asserts that, so adding an intent forces a prompt update.
INTENT_GUIDANCE: dict[Intent, str] = {
    Intent.BILLING: "Charges, invoices, payment methods, double billing, pricing questions",
    Intent.TECHNICAL: "API errors, 5xx responses, bugs, webhooks, outages, SSO/SAML setup",
    Intent.ACCOUNT: (
        "Password resets, 2FA, login problems, closing an account, cancelling a "
        "subscription, data deletion requests"
    ),
    Intent.FEATURE_REQUEST: "Requests for new functionality or enhancements",
    Intent.REFUND: "Money back, reimbursements, prorated refund and credit requests",
    Intent.COMPLAINT: "Angry escalations, poor service quality, legal threats",
    Intent.GENERAL: "Office hours and other general inquiries",
    Intent.UNKNOWN: "Ambiguous, unclassifiable, or gibberish text",
}


def build_system_prompt() -> str:
    """Build the classifier prompt from the Intent enum, so the two cannot drift."""
    lines = [f"- {intent.value}: {INTENT_GUIDANCE[intent]}" for intent in Intent]
    taxonomy = "\n".join(lines)
    return (
        "You are an expert customer support intent classifier.\n"
        "Classify the incoming customer message into exactly ONE of these canonical "
        f"intents:\n{taxonomy}\n\n"
        "Use 'unknown' when the message does not clearly fit any other intent — "
        "abstaining is preferred over guessing.\n"
        "Provide a confidence score between 0.0 and 1.0, and brief reasoning.\n"
    )


SYSTEM_PROMPT = build_system_prompt()

# Ordered most-specific first: the first matching rule wins.
# Cancellation and data-deletion language belongs to ACCOUNT, not REFUND — asking to
# close an account is not asking for money back (BRD FR-3.1; there is no
# `cancellation` intent in the 8-intent taxonomy).
_KEYWORD_RULES: list[tuple[Intent, str, tuple[str, ...]]] = [
    (
        Intent.COMPLAINT,
        "complaint",
        ("terrible", "worst", "complain", "complaint", "ruined", "unacceptable", "outrageous"),
    ),
    (
        Intent.ACCOUNT,
        "account",
        (
            "cancel my",
            "cancel the",
            "cancel subscription",
            "cancel my subscription",
            "close account",
            "close my account",
            "delete my account",
            "erase my data",
            "delete my data",
            "login",
            "log in",
            "password",
            "2fa",
        ),
    ),
    (Intent.REFUND, "refund", ("refund", "money back", "reimbursement")),
    (Intent.BILLING, "billing", ("bill", "invoice", "charge", "charged", "pricing")),
    (
        Intent.TECHNICAL,
        "technical",
        (
            "api",
            "error",
            "500",
            "504",
            "bug",
            "crash",
            "webhook",
            "timing out",
            "sso",
            "saml",
            "okta",
            "soc2",
        ),
    ),
    (Intent.FEATURE_REQUEST, "feature_request", ("feature request", "would be great if")),
]


def _fallback_classify(text: str) -> tuple[Intent, float]:
    """Deterministic keyword classifier. Confidences come from config, never from code."""
    confidences = thresholds_config().get("fallback_confidences", {})
    text_lower = text.lower()

    for intent, config_key, keywords in _KEYWORD_RULES:
        if any(kw in text_lower for kw in keywords):
            return intent, float(confidences.get(config_key, 0.60))

    return Intent.UNKNOWN, float(confidences.get("unknown", 0.40))


async def classify_intent(
    text: str, router: LLMRouter | None = None
) -> tuple[Intent, float, LLMCall | None]:
    """Classify message intent using structured LLM output, with a keyword fallback.

    ``text`` must already be PII-redacted — the caller owns that.
    """
    if not text or not text.strip():
        return Intent.UNKNOWN, 0.0, None

    if router is None:
        intent, conf = _fallback_classify(text)
        return intent, conf, None

    try:
        output, call = await router.complete_structured(
            purpose="classification",
            system=SYSTEM_PROMPT,
            user=text,
            schema=ClassificationOutput,
        )
    except (AllProvidersFailed, ProviderError, TransientError) as err:
        # Every provider failed. Degrade to keyword rules rather than dropping the
        # message, and record why: a silent degradation is worse than a slow one.
        logger.warning("classification_llm_failed_using_keyword_fallback", error=str(err))
        intent, conf = _fallback_classify(text)
        return intent, conf, None

    return output.intent, output.confidence, call
