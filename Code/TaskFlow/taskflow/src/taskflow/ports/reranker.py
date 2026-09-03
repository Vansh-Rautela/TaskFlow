"""Reranker port."""

from typing import Protocol

from taskflow.domain.models import ScoredChunk


class Reranker(Protocol):
    """Services depend on this; only adapters/rerank implements it.

    Fusion orders candidates by rank agreement between two retrievers. A reranker
    reads the query and the passage together and says how well one answers the other
    — the judgement fusion cannot make.
    """

    def rerank(self, query: str, chunks: list[ScoredChunk], limit: int) -> list[ScoredChunk]:
        """Return the best `limit` chunks, `rerank_score` set, highest first."""
        ...
