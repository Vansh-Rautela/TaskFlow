"""Retrieval.

    query -> expansion -> hybrid search (dense + BM25, RRF) -> rerank -> sufficiency

Fusion orders candidates by rank agreement; the reranker then reads the query and
each passage together and re-orders on how well one answers the other. Only the
survivors of that reach the drafting model.
"""

import time

from taskflow.config.settings import retrieval_config
from taskflow.domain.models import RetrievalResult
from taskflow.ports.reranker import Reranker
from taskflow.ports.vector_store import VectorStore
from taskflow.services.retrieve.injection import detect_context_injection
from taskflow.services.retrieve.query_builder import build_query
from taskflow.services.retrieve.sufficiency import evaluate_sufficiency


async def retrieve_context(
    query: str,
    tenant_id: str,
    vector_store: VectorStore,
    reranker: Reranker | None = None,
) -> RetrievalResult:
    """Execute hybrid retrieval and return an enriched `RetrievalResult`.

    Without a `reranker` the fused order stands and the scores are RRF rank scores,
    which encode position only. Sufficiency is far weaker in that mode — see
    `sufficiency.evaluate_sufficiency`.
    """
    cfg = retrieval_config()
    candidate_k = int(cfg["candidate_k"])
    final_k = int(cfg["rerank_k"])

    start_ns = time.perf_counter_ns()
    expanded_query = build_query(query)

    result = vector_store.search(query=expanded_query, tenant_id=tenant_id, limit=candidate_k)

    if reranker is not None:
        # The reranker gets the customer's own words. Alias expansion is a recall aid
        # for lexical and embedding match; a cross-encoder trained on natural queries
        # reads the padding as noise.
        chunks = reranker.rerank(query, result.chunks, limit=final_k)
    else:
        chunks = result.chunks[:final_k]

    elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000

    sufficient, gap_reason, top_score, support_count = evaluate_sufficiency(chunks)

    return RetrievalResult(
        query_used=expanded_query,
        chunks=chunks,
        sufficient=sufficient,
        gap_reason=gap_reason,
        suspicious_context=detect_context_injection(chunks),
        latency_ms=elapsed_ms,
        top_score=top_score,
        support_count=support_count,
    )
