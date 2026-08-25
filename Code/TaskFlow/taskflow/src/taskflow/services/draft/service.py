"""Context-Grounded LLM Drafting Service for Phase P7.

Formats retrieved Knowledge Base chunks into prompt context and uses LLMRouter to generate
tailored support responses with explicit citations [chunk_id].
"""

from taskflow.config.settings import thresholds_config
from taskflow.domain.enums import Intent
from taskflow.domain.models import Citation, DraftOutput, InboundMessage, LLMCall, RetrievalResult
from taskflow.ports.llm import LLMRouter

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
    """Generate a context-grounded response draft using LLMRouter."""
    context_text = _format_context(retrieval)

    user_prompt = f"""Customer Message:
{msg.body_text}

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
    except Exception:
        # Fall back to a safe template draft when LLM providers fail or are unconfigured.
        # Quality gates will automatically route this to HUMAN_REVIEW.
        first_chunk = retrieval.chunks[0].chunk if (retrieval and retrieval.chunks) else None
        citations: list[Citation] = []
        citation_ref = ""
        if first_chunk:
            citations = [Citation(chunk_id=first_chunk.chunk_id, doc_title=first_chunk.title)]
            citation_ref = f" [{first_chunk.chunk_id}]"

        return DraftOutput(
            response_text=f"Thank you for reaching out regarding your {intent.value} request.{citation_ref} Our support team has logged this request.",
            citations=citations,
            tone="friendly",
            complexity="simple",
            draft_confidence=thresholds_config()["fallback_confidences"].get("draft", 0.50),
        ), None
