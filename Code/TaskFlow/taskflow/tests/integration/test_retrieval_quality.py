"""Retrieval quality against a real index.

These tests build an embedded Qdrant collection and run the real encoders, so they
assert the properties that make the retrieval pipeline worth having:

  - the sparse branch finds an exact term the dense branch cannot,
  - reranking reorders fused candidates onto the one that answers the question,
  - the reranker's score separates a relevant corpus from an irrelevant query,

which is what lets `min_top_score` in thresholds.yaml mean something.

Marked `integration`: they load models from disk and are excluded from `make test`.
"""

import pytest

from taskflow.adapters.rerank.cross_encoder import CrossEncoderReranker
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.domain.models import DocumentChunk
from taskflow.services.retrieve.service import retrieve_context

pytestmark = [pytest.mark.integration, pytest.mark.slow]

TENANT = "test-tenant"

CORPUS = [
    DocumentChunk(
        chunk_id="KB-BILLING-001:0",
        doc_id="KB-BILLING-001",
        ordinal=0,
        title="Supported Payment Methods",
        section_heading="Wire Transfer",
        text=(
            "Wire transfer is available to qualifying Enterprise customers, typically "
            "under a signed agreement. Contact your account manager to enable it."
        ),
    ),
    DocumentChunk(
        chunk_id="KB-BILLING-002:0",
        doc_id="KB-BILLING-002",
        ordinal=0,
        title="Billing Cycle",
        section_heading="Renewal Dates",
        text=(
            "Subscriptions renew on the anniversary of the start date. Monthly plans "
            "renew every month and annual plans renew every twelve months."
        ),
    ),
    DocumentChunk(
        chunk_id="KB-REFUND-001:0",
        doc_id="KB-REFUND-001",
        ordinal=0,
        title="Standard Refund Policy",
        section_heading="Eligibility Window",
        text=(
            "Refund requests must be submitted within 30 days of the charge. Requests "
            "outside that window require an exception approval."
        ),
    ),
    DocumentChunk(
        chunk_id="KB-TECH-014:0",
        doc_id="KB-TECH-014",
        ordinal=0,
        title="Troubleshooting Failed Payments",
        section_heading="Decline Codes",
        text=(
            "A payment declined with code ERR-4021 means the issuing bank rejected the "
            "charge. Ask the customer to contact their bank and retry."
        ),
    ),
]


@pytest.fixture(scope="module")
def store() -> QdrantVectorStore:
    s = QdrantVectorStore(location=":memory:")
    s.setup_collection()
    s.ingest(CORPUS, tenant_id=TENANT)
    return s


@pytest.fixture(scope="module")
def reranker() -> CrossEncoderReranker:
    return CrossEncoderReranker()


async def _docs(query: str, store, reranker=None) -> list[str]:
    result = await retrieve_context(query, TENANT, vector_store=store, reranker=reranker)
    return [sc.chunk.doc_id for sc in result.chunks]


@pytest.mark.asyncio
async def test_sparse_branch_finds_an_exact_code_the_dense_branch_would_miss(store):
    """An opaque error code has no semantics to embed — this is why BM25 is here."""
    assert (await _docs("ERR-4021", store))[0] == "KB-TECH-014"


@pytest.mark.asyncio
async def test_reranking_promotes_the_document_that_answers_the_question(store, reranker):
    query = "Can we pay by wire transfer on our Enterprise plan?"
    assert (await _docs(query, store, reranker))[0] == "KB-BILLING-001"


@pytest.mark.asyncio
async def test_reranking_preserves_a_correct_fused_ordering(store, reranker):
    """It must reorder when fusion is wrong without disturbing it when fusion is right."""
    query = "How long do I have to ask for a refund?"
    assert (await _docs(query, store, reranker))[0] == "KB-REFUND-001"


@pytest.mark.asyncio
async def test_rerank_scores_separate_a_relevant_query_from_an_irrelevant_one(store, reranker):
    """The property `min_top_score` depends on: the score has to carry magnitude.

    RRF cannot do this — its top value is the same whether the best candidate was a
    perfect answer or the least bad of twenty irrelevant ones.
    """
    relevant = await retrieve_context(
        "When does my annual subscription renew?", TENANT, vector_store=store, reranker=reranker
    )
    irrelevant = await retrieve_context(
        "What is the airspeed velocity of an unladen swallow?",
        TENANT,
        vector_store=store,
        reranker=reranker,
    )

    # A wide margin, not a coin flip: this is the separation the threshold sits in.
    assert relevant.top_score > 0.9
    assert irrelevant.top_score < 0.1
    assert not irrelevant.sufficient
    assert "top_score_too_low" in str(irrelevant.gap_reason)


@pytest.mark.asyncio
async def test_retrieval_returns_rerank_k_chunks_with_scores_attached(store, reranker):
    result = await retrieve_context("refund window", TENANT, vector_store=store, reranker=reranker)
    assert result.chunks
    assert all(sc.rerank_score is not None for sc in result.chunks)
    # Highest first.
    scores = [sc.final_score for sc in result.chunks]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_tenant_filter_excludes_another_tenants_corpus(store, reranker):
    result = await retrieve_context(
        "wire transfer", "some-other-tenant", vector_store=store, reranker=reranker
    )
    assert result.chunks == []
    assert result.gap_reason == "no_chunks_retrieved"


@pytest.mark.asyncio
async def test_reingesting_the_same_chunks_does_not_duplicate_them(store):
    store.ingest(CORPUS, tenant_id=TENANT)
    count = store.client.count(store.collection_name).count
    assert count == len(CORPUS)
