"""Heading-aware Markdown chunking.

Splits a document on section boundaries, keeping chunks coherent and anchored to the
heading they came from. Chunk ids are derived from the document id, so re-ingesting
an unchanged document produces the same ids and the upsert is a no-op.
"""

import hashlib
from collections.abc import Iterator

from taskflow.domain.models import DocumentChunk

__all__ = ["DocumentChunk", "generate_doc_id", "split_document"]

# A word is roughly 0.75 tokens, so a token budget converts to a word budget by *0.75.
_WORDS_PER_TOKEN = 0.75


def generate_doc_id(relpath: str, title: str) -> str:
    """Generate a deterministic 16-hexdigit document ID."""
    raw = f"{relpath}\n{title}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _tail_overlap(lines: list[str], budget_words: int) -> tuple[list[str], int]:
    """The trailing lines of a finished chunk that should open the next one.

    Walks backwards while the budget allows. A single line longer than the whole
    budget yields no overlap rather than blowing through it.
    """
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        words = len(line.split())
        if used + words > budget_words:
            break
        kept.insert(0, line)
        used += words
    return kept, used


def split_document(
    relpath: str,
    title: str,
    markdown_content: str,
    max_tokens: int = 450,
    overlap: int = 60,
    doc_id: str | None = None,
) -> Iterator[DocumentChunk]:
    """Split markdown into roughly `max_tokens`-sized chunks, anchored to headings.

    `doc_id` overrides the path-derived id — knowledge-base documents declare their
    own in frontmatter, and the evaluation corpora reference that id.
    """
    doc_id = doc_id or generate_doc_id(relpath, title)
    max_words = int(max_tokens * _WORDS_PER_TOKEN)
    overlap_words = int(overlap * _WORDS_PER_TOKEN)

    heading: str | None = None
    lines_buffer: list[str] = []
    word_count = 0
    ordinal = 0

    def _emit(ord_num: int) -> DocumentChunk:
        return DocumentChunk(
            chunk_id=f"{doc_id}:{ord_num}",
            doc_id=doc_id,
            title=title,
            section_heading=heading,
            text="\n".join(lines_buffer).strip(),
            ordinal=ord_num,
        )

    for line in markdown_content.splitlines():
        is_heading = line.startswith("#")

        # A section boundary closes the current chunk once it holds enough to stand
        # alone; below that, the new section joins it rather than leaving a stub.
        if is_heading:
            level = len(line) - len(line.lstrip("#"))
            if level >= 2 and word_count > (max_words // 2) and lines_buffer:
                yield _emit(ordinal)
                ordinal += 1
                lines_buffer, word_count = _tail_overlap(lines_buffer, overlap_words)
            heading = line.lstrip("# ").strip()

        lines_buffer.append(line)
        word_count += len(line.split())

        # Hard cap: split regardless of where the headings fall.
        if word_count >= max_words:
            yield _emit(ordinal)
            ordinal += 1
            lines_buffer, word_count = _tail_overlap(lines_buffer, overlap_words)

    if lines_buffer and "\n".join(lines_buffer).strip():
        yield _emit(ordinal)
