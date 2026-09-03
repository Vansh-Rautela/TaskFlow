"""Unit tests for Phase P7 Drafting & Validation Services."""

from datetime import UTC, datetime

import pytest

from taskflow.adapters.llm.router import ProviderRouter
from taskflow.domain.enums import Intent
from taskflow.domain.models import (
    Chunk,
    DraftOutput,
    InboundMessage,
    RetrievalResult,
    ScoredChunk,
)
from taskflow.services.draft.service import generate_draft
from taskflow.services.validate.citations import validate_citations
from taskflow.services.validate.grounding import validate_grounding
from taskflow.services.validate.pii_leak import validate_pii
from taskflow.services.validate.runner import run_validators
from tests.unit.test_providers import MockSuccessProvider


@pytest.fixture
def sample_retrieval():
    chunk = Chunk(
        chunk_id="chunk-001",
        doc_id="doc-001",
        title="Refund Policy",
        section="30-Day Limit",
        text="Standard 30-day refund policy applies to annual subscriptions.",
        version="1.0",
        source_path="",
        tenant_id="test",
    )
    return RetrievalResult(
        query_used="refund policy",
        chunks=[ScoredChunk(chunk=chunk, rrf_score=0.90)],
        sufficient=True,
        latency_ms=10,
    )


@pytest.mark.asyncio
async def test_generate_draft(sample_retrieval):
    """Generate draft formatted with context and citations."""
    mock_p = MockSuccessProvider()
    router = ProviderRouter(providers={"claude": mock_p})
    msg = InboundMessage(
        message_id="m1",
        dedupe_key="d1",
        tenant_id="test",
        channel="console",
        sender="s@example.com",
        subject=None,
        body_text="Can I get a refund?",
        body_redacted="Can I get a refund?",
        provider_message_id="p1",
        received_at=datetime.now(UTC),
    )

    draft, _call = await generate_draft(msg, Intent.REFUND, sample_retrieval, router)
    assert isinstance(draft, DraftOutput)
    assert draft.response_text is not None


@pytest.mark.asyncio
async def test_citations_validator_pass(sample_retrieval):
    """Citation validator passes when cited chunk_id exists in retrieval."""
    draft = DraftOutput(
        response_text="Standard 30-day refund policy applies [chunk-001].",
        citations=[{"chunk_id": "chunk-001", "doc_title": "Refund Policy"}],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    res = await validate_citations(draft, sample_retrieval)
    assert res.passed
    assert res.score == 1.0


@pytest.mark.asyncio
async def test_citations_validator_fail(sample_retrieval):
    """Citation validator fails when cited chunk_id is missing from retrieval."""
    draft = DraftOutput(
        response_text="Fake policy statement [chunk-999].",
        citations=[{"chunk_id": "chunk-999", "doc_title": "Fake Policy"}],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    res = await validate_citations(draft, sample_retrieval)
    assert not res.passed
    assert "Unresolved citations" in res.reason


@pytest.mark.asyncio
async def test_pii_validator_clean():
    """PII validator passes on clean response text."""
    draft = DraftOutput(
        response_text="Thank you for reaching out to support.",
        citations=[],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    res = await validate_pii(draft)
    assert res.passed


@pytest.mark.asyncio
async def test_pii_validator_detect_cc():
    """PII validator fails when credit card format is present."""
    draft = DraftOutput(
        response_text="Your credit card is 4111 2222 3333 4444.",
        citations=[],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    res = await validate_pii(draft)
    assert not res.passed
    assert "Credit Card" in res.reason


@pytest.mark.asyncio
async def test_pii_validator_detect_secret_key():
    """PII validator fails when secret API key is present."""
    draft = DraftOutput(
        response_text="Use secret key sk_notarealkey_0000000000000000 for auth.",
        citations=[],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    res = await validate_pii(draft)
    assert not res.passed
    assert "secret" in res.reason.lower()


@pytest.mark.asyncio
async def test_grounding_validator_pass(sample_retrieval):
    """Grounding validator passes when draft words overlap with context text."""
    draft = DraftOutput(
        response_text="Standard 30-day refund policy applies to annual subscriptions.",
        citations=[],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    res = await validate_grounding(draft, sample_retrieval)
    assert res.passed
    assert res.score >= 0.80


@pytest.mark.asyncio
async def test_run_validators_parallel(sample_retrieval):
    """run_validators executes pii, citations, and grounding concurrently."""
    draft = DraftOutput(
        response_text="Standard 30-day refund policy applies [chunk-001].",
        citations=[{"chunk_id": "chunk-001", "doc_title": "Refund Policy"}],
        tone="friendly",
        complexity="simple",
        draft_confidence=0.9,
    )
    results = await run_validators(draft, sample_retrieval)
    assert len(results) == 3
    names = {r.validator_name for r in results}
    assert names == {"pii_leak", "citations", "grounding"}
