"""Sufficiency evaluation.

Decides whether the retrieved chunks are enough evidence to draft from, or whether
the message should escalate on a retrieval gap.

The check is only as meaningful as the score it reads. `ScoredChunk.final_score` is
the reranker's calibrated relevance when a reranker ran, and an RRF rank score when
one did not. RRF encodes rank position only — its top value is the same whether the
best candidate was a perfect answer or the least bad of twenty irrelevant ones — so
without reranking no threshold on it can separate a good retrieval from a bad one.
"""

from taskflow.config.settings import thresholds_config
from taskflow.domain.models import ScoredChunk


def evaluate_sufficiency(
    chunks: list[ScoredChunk],
) -> tuple[bool, str | None, float, int]:
    """Evaluate retrieval sufficiency against the thresholds in thresholds.yaml.

    Returns:
        (sufficient, gap_reason, top_score, support_count)
    """
    cfg = thresholds_config().get("retrieval", {})
    min_top_score = float(cfg.get("min_top_score", 0.55))
    min_support_score = float(cfg.get("min_support_score", 0.20))
    min_support_count = int(cfg.get("min_support_count", 2))

    if not chunks:
        return False, "no_chunks_retrieved", 0.0, 0

    top_score = max(c.final_score for c in chunks)
    support_count = sum(1 for c in chunks if c.final_score >= min_support_score)

    if top_score < min_top_score:
        return (
            False,
            f"top_score_too_low ({top_score:.3f} < {min_top_score:.3f})",
            top_score,
            support_count,
        )

    if support_count < min_support_count:
        return (
            False,
            f"insufficient_support_count ({support_count} < {min_support_count})",
            top_score,
            support_count,
        )

    return True, None, top_score, support_count
