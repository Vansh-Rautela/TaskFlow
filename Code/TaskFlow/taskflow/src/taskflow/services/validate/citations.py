"""Citation resolution validator for Phase P7.

Extracts inline bracketed citations [chunk_id] and structured citations, verifying that
every cited ID exists within the retrieved context chunks.
"""

import re

from taskflow.config.settings import thresholds_config
from taskflow.domain.models import DraftOutput, RetrievalResult, ValidatorResult

CITATION_PATTERN = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")


async def validate_citations(
    draft: DraftOutput, retrieval: RetrievalResult | None
) -> ValidatorResult:
    """Validate that all citations in the draft exist in the retrieved context chunks."""
    retrieved_chunk_ids = (
        {scored.chunk.chunk_id for scored in retrieval.chunks} if retrieval else set()
    )

    # 1. Collect structured citations
    cited_ids = {c.chunk_id for c in draft.citations} if draft.citations else set()

    # 2. Extract inline citations [chunk_id] from response_text
    inline_ids = set(CITATION_PATTERN.findall(draft.response_text or ""))
    all_cited_ids = cited_ids | inline_ids

    if not all_cited_ids:
        # If no citations were generated when retrieval context was present, flag for review
        if retrieval and retrieval.chunks:
            return ValidatorResult(
            validator_name="citations",
            passed=True,
            score=thresholds_config()["citation_validation_threshold"],
            reason="No inline citations found in draft",
            blocking=False,
            )
        return ValidatorResult(
            validator_name="citations", passed=True, score=1.0, reason="ok", blocking=False
        )

    unresolved = [cid for cid in all_cited_ids if cid not in retrieved_chunk_ids]

    if unresolved:
        coverage = (len(all_cited_ids) - len(unresolved)) / len(all_cited_ids)
        return ValidatorResult(
            validator_name="citations",
            passed=False,
            score=round(coverage, 2),
            reason=f"Unresolved citations: {', '.join(unresolved)}",
            blocking=True,
        )

    return ValidatorResult(
        validator_name="citations", passed=True, score=1.0, reason="ok", blocking=False
    )
