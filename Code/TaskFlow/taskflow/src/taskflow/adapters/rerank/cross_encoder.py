"""Cross-encoder reranker.

FastEmbed runs the model locally on ONNX — the same dependency the embedders already
use, so this adds no server and no API key. Implements the `Reranker` port.
"""

import math

from fastembed.rerank.cross_encoder import TextCrossEncoder

from taskflow.config.settings import retrieval_config
from taskflow.domain.models import ScoredChunk


def _calibrate(logit: float) -> float:
    """Squash the model's raw logit into 0-1 so a threshold can be reasoned about.

    The cross-encoder emits an unbounded relevance logit. `thresholds.yaml` states a
    floor as a probability, and `retrieval_relevance` is a 0-1 quality signal, so the
    conversion has to happen somewhere; doing it here keeps every score that leaves
    this adapter on one comparable scale.
    """
    return 1.0 / (1.0 + math.exp(-logit))


class CrossEncoderReranker:
    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or str(retrieval_config()["reranker_model"])
        self.model = TextCrossEncoder(self.model_name)

    def rerank(self, query: str, chunks: list[ScoredChunk], limit: int) -> list[ScoredChunk]:
        """Score every candidate against the query and return the best `limit`."""
        if not chunks:
            return []

        # The passage text alone, with no title or heading prefix. That prefix is what
        # the *embedder* needs to place a chunk inside its document; a cross-encoder is
        # trained on bare (query, passage) pairs and scores slightly better without it
        # — measured at Recall@5 95.16 against 94.30 over the 351-query corpus.
        scores = list(self.model.rerank(query, [sc.chunk.text for sc in chunks]))

        reranked = [
            sc.model_copy(update={"rerank_score": _calibrate(float(score))})
            for sc, score in zip(chunks, scores, strict=True)
        ]
        reranked.sort(key=lambda sc: sc.final_score, reverse=True)
        return reranked[:limit]
