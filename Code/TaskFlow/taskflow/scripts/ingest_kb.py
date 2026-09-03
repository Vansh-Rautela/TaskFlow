#!/usr/bin/env python3
"""Knowledge-base ingestion.

Reads every markdown file under data/knowledge_base/, extracts its YAML frontmatter,
splits it into heading-anchored chunks, and upserts them into the embedded Qdrant
collection with both dense and sparse vectors.

Chunk ids are deterministic, so re-running is idempotent for unchanged documents.
That is not the same as being current: a chunk that no longer exists in the source
is not removed by an upsert. `--recreate` drops the collection first and is the
correct way to rebuild after documents are edited, split, or deleted.

    uv run python scripts/ingest_kb.py [--recreate]
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.config.settings import settings
from taskflow.services.retrieve.chunking import split_document

KB_DIR = Path("data/knowledge_base")


def parse_markdown_with_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and the remaining markdown body."""
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, content
    return metadata, parts[2].strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop the collection before ingesting, clearing chunks whose source is gone.",
    )
    args = parser.parse_args()

    if not KB_DIR.exists():
        print(f"Error: {KB_DIR} does not exist.", file=sys.stderr)
        return 1

    print("Loading embedding models and opening the Qdrant collection...")
    store = QdrantVectorStore()
    store.setup_collection(recreate=args.recreate)
    if args.recreate:
        print("Collection recreated from scratch.")

    tenant_id = settings().taskflow_tenant_id
    all_chunks = []
    total_docs = 0

    for file_path in sorted(KB_DIR.rglob("*.md")):
        relpath = file_path.relative_to(KB_DIR).as_posix()
        metadata, body = parse_markdown_with_frontmatter(file_path.read_text(encoding="utf-8"))
        title = metadata.get("title", file_path.stem)

        # The frontmatter `document_id` is the id the evaluation corpora reference,
        # so it wins over the path-derived one.
        chunks = list(split_document(relpath, title, body, doc_id=metadata.get("document_id")))
        all_chunks.extend(chunks)
        total_docs += 1
        print(f"  {relpath}: {len(chunks)} chunks")

    if not all_chunks:
        print(f"Error: no chunks produced from {KB_DIR}.", file=sys.stderr)
        return 1

    print(f"\n{total_docs} documents, {len(all_chunks)} chunks. Embedding and upserting...")
    store.ingest(all_chunks, tenant_id=tenant_id)

    print(f"Done: {len(all_chunks)} chunks in collection `{store.collection_name}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
