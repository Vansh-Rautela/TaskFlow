"""Vector store port."""

from typing import Protocol

from taskflow.domain.models import DocumentChunk, RetrievalResult


class VectorStore(Protocol):
    """Services depend on this; only adapters/vector implements it."""

    def setup_collection(self, recreate: bool = False) -> None: ...

    def ingest(self, chunks: list[DocumentChunk], tenant_id: str) -> None: ...

    def search(self, query: str, tenant_id: str, limit: int = 5) -> RetrievalResult: ...
