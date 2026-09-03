"""Context-Grounded LLM Drafting Service for Phase P7.

Formats retrieved Knowledge Base chunks into prompt context and uses LLMRouter to generate
tailored support responses with explicit citations [chunk_id].
"""

import structlog

from taskflow.config.settings import thresholds_config
from taskflow.domain.enums import Intent
from taskflow.domain.errors import AllProvidersFailed, ProviderError, TransientError
from taskflow.domain.models import DraftOutput, InboundMessage, LLMCall, RetrievalResult
from taskflow.ports.llm import LLMRouter

logger = structlog.get_logger()

SYSTEM_PROMPT = """You are an expert customer support agent writing a professional email response.
Follow these strict guidelines:
1. Base your response EXCLUSIVELY on the provided Knowledge Base Context chunks.
2. Include inline bracketed citations matching chunk IDs for every policy or fact (e.g. [chunk_id]). Ensure EVERY citation included in the `citations` list is actually referenced inline in the text.
3. Keep the tone professional, helpful, and concise. Your chosen tone MUST be one of: "formal", "friendly", "apologetic", "neutral", or "technical". Do not use "professional" as the tone string.
4. Never invent details, dates, or terms not supported by the context.
"""


def _format_context(retrieval: RetrievalResult | None) -> str:
    if not retrieval or not retrieval.chunks:
        return "No relevant Knowledge Base context found."

    formatted_blocks = []
    for sc in retrieval.chunks:
        c = sc.chunk
        section_info = f" | Section: {c.section}" if c.section else ""
        formatted_blocks.append(
            f"[Chunk ID: {c.chunk_id} | Title: {c.title}{section_info}]\n{c.text}"
        )
    return "\n\n".join(formatted_blocks)


async def generate_draft(
    msg: InboundMessage,
    intent: Intent,
    retrieval: RetrievalResult,
    router: LLMRouter,
) -> tuple[DraftOutput, LLMCall | None]:
    """Generate a context-grounded response draft using LLMRouter.

    Only ``msg.body_redacted`` is ever placed in the prompt. The raw ``body_text``
    must not reach a provider — see BRD NFR-3.1 and R-1.
    """
    context_text = _format_context(retrieval)

    user_prompt = f"""Customer Message:
{msg.body_redacted}

Detected Intent: {intent.value}

Knowledge Base Context:
{context_text}
"""

    try:
        draft, _call = await router.complete_structured(
            purpose="draft",
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=DraftOutput,
        )
        return draft, _call
    except (AllProvidersFailed, ProviderError, TransientError) as err:
        # Every provider failed. Produce a holding reply so the message keeps moving,
        # and return None for the LLMCall — the caller uses that to tell the gates
        # that no model actually answered, so this must never auto-send.
        #
        # It carries NO citations. The previous version attached the first retrieved
        # chunk, which earned this stub a perfect citation-coverage score for
        # material it had not used; combined with the general threshold that was
        # enough to auto-send a content-free reply to a customer.
        logger.warning(
            "draft_llm_failed_using_template_fallback", intent=intent.value, error=str(err)
        )

        return DraftOutput(
            response_text=(
                f"Thank you for contacting us about your {intent.value} request. "
                "A member of our support team is reviewing it and will reply shortly."
            ),
            citations=[],
            tone="friendly",
            complexity="simple",
            draft_confidence=float(thresholds_config()["fallback_confidences"].get("draft", 0.50)),
        ), None
