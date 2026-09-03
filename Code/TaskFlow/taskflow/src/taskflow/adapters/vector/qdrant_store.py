"""Embedded Qdrant vector store.

FastEmbed supplies both encoders locally — dense (cosine) and sparse (BM25/IDF) —
so there is no server and no network call. Implements the `VectorStore` port.
"""

import uuid

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient, models

from taskflow.config.settings import retrieval_config, settings
from taskflow.domain.models import Chunk, DocumentChunk, RetrievalResult, ScoredChunk

# A deterministic namespace, so an unchanged chunk_id always maps to the same point id
# and re-ingestion is an idempotent upsert rather than a duplicate.
NAMESPACE_TASKFLOW = uuid.UUID("f0e1d2c3-b4a5-9687-7869-5a4b3c2d1e0f")

_DENSE = "dense"
_SPARSE = "bm25"


def embedding_text(title: str, section: str | None, text: str) -> str:
    """The text that gets embedded, which is not the text that gets stored.

    A chunk taken out of the middle of a document loses the context its heading gave
    it — "Wire transfer is available to qualifying Enterprise customers" does not say
    anywhere in its own body that it is about payment methods. Prefixing the document
    title and section heading puts that context back into the vector, and measurably
    improves the hard tiers (see docs/metrics/retrieval.md).

    The stored `text` stays clean: it is what the drafting model quotes and cites.
    """
    header = [title] if title else []
    if section and section != title:
        header.append(section)
    return "\n".join([*header, text])


class QdrantVectorStore:
    def __init__(self, collection_name: str | None = None, location: str | None = None) -> None:
        cfg = retrieval_config()
        self.client = (
            QdrantClient(location=location)
            if location
            else QdrantClient(path=settings().qdrant_path)
        )
        self.collection_name = collection_name or settings().qdrant_collection
        self._dense_dim = int(cfg["dense_dim"])
        self.dense_model = TextEmbedding(str(cfg["dense_model"]))
        self.sparse_model = SparseTextEmbedding(str(cfg["sparse_model"]))

    def setup_collection(self, recreate: bool = False) -> None:
        """Create the collection and its payload index. Safe to call repeatedly.

        `recreate=True` drops it first: upserting cannot remove a chunk whose source
        document no longer produces it, so a rebuild is the only way to clear stale
        entries after the knowledge base is edited.
        """
        if recreate and self.client.collection_exists(self.collection_name):
            self.client.delete_collection(self.collection_name)
        elif self.client.collection_exists(self.collection_name):
            return

        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config={
                _DENSE: models.VectorParams(size=self._dense_dim, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={
                _SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )

        # Only tenant_id is filtered on today. Indexing fields nothing queries was
        # cost without benefit — and the embedded store ignores payload indexes anyway.
        self.client.create_payload_index(
            collection_name=self.collection_name,
            field_name="tenant_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )

    def ingest(
        self,
        chunks: list[DocumentChunk],
        tenant_id: str,
        source_path: str | None = None,
        version: str = "1.0",
    ) -> None:
        """Embed and upsert chunks. Deterministic point ids make this idempotent."""
        if not chunks:
            return

        texts = [embedding_text(c.title, c.section_heading, c.text) for c in chunks]
        dense_embeddings = list(self.dense_model.embed(texts))
        sparse_embeddings = list(self.sparse_model.embed(texts))

        points = [
            models.PointStruct(
                id=str(uuid.uuid5(NAMESPACE_TASKFLOW, c.chunk_id)),
                vector={
                    _DENSE: dense_embeddings[i].tolist(),
                    _SPARSE: models.SparseVector(
                        indices=sparse_embeddings[i].indices.tolist(),
                        values=sparse_embeddings[i].values.tolist(),
                    ),
                },
                payload={
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "title": c.title,
                    "section_heading": c.section_heading,
                    "ordinal": c.ordinal,
                    "tenant_id": tenant_id,
                    "text": c.text,
                    "source_path": source_path or "",
                    "version": version,
                },
            )
            for i, c in enumerate(chunks)
        ]

        self.client.upsert(collection_name=self.collection_name, points=points)

    def search(self, query: str, tenant_id: str, limit: int = 5) -> RetrievalResult:
        """Hybrid dense + sparse query, fused server-side with RRF."""
        prefetch_limit = int(retrieval_config()["prefetch_k"])
        q_dense = next(iter(self.dense_model.embed([query]))).tolist()
        q_sparse = next(iter(self.sparse_model.embed([query])))

        tenant_filter = models.Filter(
            must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))]
        )

        results = self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(
                    query=q_dense, using=_DENSE, limit=prefetch_limit, filter=tenant_filter
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=q_sparse.indices.tolist(), values=q_sparse.values.tolist()
                    ),
                    using=_SPARSE,
                    limit=prefetch_limit,
                    filter=tenant_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
        )

        chunks = [
            ScoredChunk(
                chunk=Chunk(
                    chunk_id=(p.payload or {}).get("chunk_id", "unknown"),
                    doc_id=(p.payload or {}).get("doc_id", "unknown"),
                    title=(p.payload or {}).get("title", ""),
                    section=(p.payload or {}).get("section_heading"),
                    text=(p.payload or {}).get("text", ""),
                    version=(p.payload or {}).get("version", "1.0"),
                    source_path=(p.payload or {}).get("source_path", ""),
                    tenant_id=tenant_id,
                ),
                rrf_score=p.score,
            )
            for p in results.points
        ]

        # `sufficient` and `latency_ms` are decided by services.retrieve, which owns
        # the thresholds and the clock. The adapter reports what it found, not what
        # that means.
        return RetrievalResult(query_used=query, chunks=chunks, sufficient=False, latency_ms=0)
