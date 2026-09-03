#!/usr/bin/env python3
"""Retrieval evaluation.

Scores retrieval over the tiered evaluation corpora and reports Recall@1/@3/@5,
MRR@10, latency, and whether the sufficiency check can actually tell a good
retrieval from a bad one.

Three reporting rules this script follows:

  - **Distinct queries only.** `golden_eval.jsonl` contains 7 unique questions
    repeated ten times each. Scoring it reports the mean of seven queries while
    looking like a 70-query result. The tier corpora carry 351 distinct queries
    with per-query ground truth, so they are the default set.
  - **A query with no ground truth is excluded**, never counted as a hit.
  - **Recall is measured over documents, not chunks.** Five retrieved chunks from
    one document is one document at rank 1, not five chances to be right.

    uv run python scripts/eval_retrieval.py
    uv run python scripts/eval_retrieval.py --dataset golden_eval
"""

import argparse
import asyncio
import json
import statistics
from pathlib import Path

from taskflow.adapters.rerank.cross_encoder import CrossEncoderReranker
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.config.settings import retrieval_config, settings, thresholds_config
from taskflow.services.retrieve.service import retrieve_context

DATASET_DIR = Path("data/datasets")
METRICS_OUT_PATH = Path("docs/metrics/retrieval.md")

TIER_FILES = ["tier-1.jsonl", "tier-2.jsonl", "tier-3.jsonl", "tier-4.jsonl"]


def _load(filename: str) -> list[dict]:
    """Load one dataset, normalised to {query, expected, tier}."""
    path = DATASET_DIR / filename
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        # The tier corpora and golden_eval use different field names for the
        # same two things. Normalise rather than teaching the scorer both.
        query = raw.get("query") or raw.get("text") or ""
        expected = raw.get("ground_truth_documents") or raw.get("expected_doc_ids") or []
        if not query or not expected:
            continue
        rows.append(
            {"query": query, "expected": set(expected), "tier": raw.get("complexity_tier", 0)}
        )
    return rows


def _summarise(values: list[float]) -> str:
    if not values:
        return "n=0"
    return (
        f"n={len(values)} min={min(values):.3f} "
        f"median={statistics.median(values):.3f} max={max(values):.3f}"
    )


def _sweep(hit_top: list[float], miss_top: list[float]) -> list[tuple[float, float, float]]:
    """What each candidate `min_top_score` would actually do.

    Returns (threshold, % of good retrievals wrongly escalated, % of bad ones let
    through). Choosing the floor is a trade between those two columns, and this is
    the table that shows the trade instead of asserting a number.
    """
    rows = []
    for step in range(1, 20):
        t = step / 20
        escalated_good = sum(1 for v in hit_top if v < t) / len(hit_top) * 100 if hit_top else 0.0
        passed_bad = sum(1 for v in miss_top if v >= t) / len(miss_top) * 100 if miss_top else 0.0
        rows.append((t, escalated_good, passed_bad))
    return rows


def _rank_documents(chunks) -> list[str]:
    """Collapse retrieved chunks to a ranked, de-duplicated document list."""
    seen: set[str] = set()
    docs: list[str] = []
    for scored in chunks:
        doc_id = scored.chunk.doc_id
        if doc_id not in seen:
            seen.add(doc_id)
            docs.append(doc_id)
    return docs


async def evaluate(dataset: str, rerank: bool = True) -> int:
    files = ["golden_eval.jsonl"] if dataset == "golden_eval" else TIER_FILES

    groups: dict[str, list[dict]] = {}
    for filename in files:
        rows = _load(filename)
        if rows:
            groups[filename.removesuffix(".jsonl")] = rows

    if not groups:
        print(f"Error: no scorable rows found in {files}. Run scripts/generate_datasets.py.")
        return 1

    total_rows = sum(len(r) for r in groups.values())
    distinct = len({row["query"] for rows in groups.values() for row in rows})
    print(
        f"Loaded {total_rows} scorable rows ({distinct} distinct queries) from {len(groups)} file(s)."
    )
    if distinct < total_rows:
        print(
            f"  NOTE: {total_rows - distinct} rows are duplicate queries. Metrics below are the\n"
            f"  mean over {total_rows} rows, but only {distinct} distinct questions are being asked."
        )

    store = QdrantVectorStore()
    reranker = CrossEncoderReranker() if rerank else None
    tenant = settings().taskflow_tenant_id
    print(f"Reranking: {'on — ' + reranker.model_name if reranker else 'off (fusion order)'}")

    latencies: list[int] = []
    hit_top: list[float] = []
    miss_top: list[float] = []
    supports: list[int] = []
    sufficient_and_hit = 0
    sufficient_but_miss = 0
    gap_count = 0
    per_group: dict[str, dict] = {}

    for name, rows in groups.items():
        agg = {"r1": 0, "r3": 0, "r5": 0, "mrr": 0.0, "n": len(rows)}
        for row in rows:
            result = await retrieve_context(
                row["query"], tenant, vector_store=store, reranker=reranker
            )
            latencies.append(result.latency_ms)
            supports.append(result.support_count)
            if not result.sufficient:
                gap_count += 1

            docs = _rank_documents(result.chunks)
            expected = row["expected"]

            if any(d in expected for d in docs[:1]):
                agg["r1"] += 1
            if any(d in expected for d in docs[:3]):
                agg["r3"] += 1

            hit = any(d in expected for d in docs[:5])
            if hit:
                agg["r5"] += 1
                hit_top.append(result.top_score)
            else:
                miss_top.append(result.top_score)

            if result.sufficient:
                if hit:
                    sufficient_and_hit += 1
                else:
                    sufficient_but_miss += 1

            for rank, doc_id in enumerate(docs[:10], start=1):
                if doc_id in expected:
                    agg["mrr"] += 1.0 / rank
                    break
        per_group[name] = agg

    total = total_rows
    recall_1 = sum(a["r1"] for a in per_group.values()) / total * 100.0
    recall_3 = sum(a["r3"] for a in per_group.values()) / total * 100.0
    recall_5 = sum(a["r5"] for a in per_group.values()) / total * 100.0
    mrr_10 = sum(a["mrr"] for a in per_group.values()) / total
    gap_rate = gap_count / total * 100.0
    avg_latency = sum(latencies) / total

    cfg = thresholds_config()["retrieval"]
    cfg_r = retrieval_config()
    reranker_line = (
        f"`{reranker.model_name}`" if reranker else "**off** — fused order scored directly"
    )
    min_top = float(cfg["min_top_score"])
    false_confidence = sufficient_but_miss / total * 100.0
    separation = (
        statistics.median(hit_top) - statistics.median(miss_top) if hit_top and miss_top else 0.0
    )

    print("\n=== RETRIEVAL EVALUATION ===")
    print(f"Queries scored:      {total}")
    print(f"Recall@1:            {recall_1:.2f}%")
    print(f"Recall@3:            {recall_3:.2f}%")
    print(f"Recall@5:            {recall_5:.2f}%")
    print(f"MRR@10:              {mrr_10:.4f}")
    print(f"Gap rate:            {gap_rate:.2f}%")
    print(f"Average latency:     {avg_latency:.1f} ms")

    print("\n=== BY DIFFICULTY ===")
    for name, a in per_group.items():
        n = a["n"]
        print(
            f"{name:<14} n={n:<4} R@1 {a['r1'] / n * 100:5.1f}%  "
            f"R@3 {a['r3'] / n * 100:5.1f}%  R@5 {a['r5'] / n * 100:5.1f}%  MRR {a['mrr'] / n:.3f}"
        )

    print("\n=== SUFFICIENCY DISCRIMINATION ===")
    print(f"min_top_score:       {min_top}")
    print(f"top_score when an expected doc WAS retrieved:   {_summarise(hit_top)}")
    print(f"top_score when it was NOT:                      {_summarise(miss_top)}")
    print(f"Declared sufficient and correct:                {sufficient_and_hit}")
    print(f"Declared sufficient but wrong:                  {sufficient_but_miss}")
    print(f"False-confidence rate:                          {false_confidence:.2f}%")
    print(f"Median separation (hit - miss):                 {separation:+.3f}")
    if separation < 0.05:
        print(
            "\n  WARNING: the hit and miss score distributions overlap, so the\n"
            "  sufficiency check cannot distinguish a good retrieval from a bad one."
        )

    sweep = _sweep(hit_top, miss_top)
    print("\n=== min_top_score TRADE-OFF ===")
    print("  threshold   good retrievals escalated   bad retrievals passed")
    for t, esc, passed in sweep:
        marker = "  <- configured" if abs(t - min_top) < 1e-9 else ""
        print(f"  {t:>9.2f}   {esc:>23.1f}%   {passed:>19.1f}%{marker}")

    print("\n=== SUPPORT COUNT ===")
    print(
        f"  min_support_count={cfg['min_support_count']} at min_support_score={cfg['min_support_score']}"
    )
    print(f"  chunks clearing the support bar: {_summarise([float(c) for c in supports])}")

    sweep_table = "\n".join(
        f"| {t:.2f}{' **(configured)**' if abs(t - min_top) < 1e-9 else ''} "
        f"| {esc:.1f}% | {passed:.1f}% |"
        for t, esc, passed in sweep
        if 0.15 <= t <= 0.90
    )

    rows_table = "\n".join(
        f"| {name} | {a['n']} | {a['r1'] / a['n'] * 100:.1f}% | {a['r3'] / a['n'] * 100:.1f}% "
        f"| {a['r5'] / a['n'] * 100:.1f}% | {a['mrr'] / a['n']:.3f} |"
        for name, a in per_group.items()
    )

    METRICS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = f"""# Retrieval Metrics

Generated by `scripts/eval_retrieval.py`. Every figure is measured, none is asserted.

Scored over the tiered evaluation corpora — {total} queries with per-query ground
truth, rising in difficulty from tier 1 (direct factual lookup) to tier 4
(multi-document and ambiguous). Recall is measured over **documents**: retrieved
chunks are collapsed to a ranked document list first, so five chunks from one
document count as one document at rank 1.

| Metric | Value |
|---|---|
| Queries scored | {total} ({distinct} distinct) |
| Recall@1 | {recall_1:.2f}% |
| Recall@3 | {recall_3:.2f}% |
| Recall@5 | {recall_5:.2f}% |
| MRR@10 | {mrr_10:.4f} |
| Gap rate | {gap_rate:.2f}% |
| Average latency | {avg_latency:.1f} ms |

## By difficulty

| Corpus | Queries | Recall@1 | Recall@3 | Recall@5 | MRR@10 |
|---|---|---|---|---|---|
{rows_table}

## Sufficiency discrimination

The question this section answers: when the retrieval service reports `sufficient`,
is it right? A low gap rate is only good news if the check is actually discriminating.

`top_score` is the reranker's calibrated relevance for the best chunk when reranking
is on, and an RRF rank score when it is off. Only the former carries magnitude, so
only the former can support a threshold.

| Measure | Value |
|---|---|
| Configured `min_top_score` | {min_top} |
| `top_score` when an expected doc was retrieved | {_summarise(hit_top)} |
| `top_score` when none was | {_summarise(miss_top)} |
| Median separation (hit - miss) | {separation:+.3f} |
| Declared sufficient **and** correct | {sufficient_and_hit} |
| Declared sufficient **but** wrong | {sufficient_but_miss} |
| False-confidence rate | {false_confidence:.2f}% |

### Choosing `min_top_score`

What each candidate floor would do, measured over the same {total} queries. The left
column is the cost (a good retrieval sent to a human anyway), the right column is the
risk (a bad retrieval allowed to draft).

| Threshold | Good retrievals escalated | Bad retrievals passed |
|---|---|---|
{sweep_table}

## Configuration

- Vector store: embedded Qdrant, collection `{settings().qdrant_collection}`
- Query processing: normalisation + alias expansion (`services/retrieve/query_builder.py`)
- Dense: `{cfg_r["dense_model"]}` ({cfg_r["dense_dim"]}-dim cosine)
- Sparse: `{cfg_r["sparse_model"]}` (IDF), fused server-side with RRF
- Reranker: {reranker_line}
- `prefetch_k={cfg_r["prefetch_k"]}` per branch, `candidate_k={cfg_r["candidate_k"]}` fused
  candidates reranked down to `rerank_k={cfg_r["rerank_k"]}`
"""
    await asyncio.to_thread(METRICS_OUT_PATH.write_text, report, "utf-8")
    print(f"\nPublished report to {METRICS_OUT_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default="tiers",
        choices=["tiers", "golden_eval"],
        help="Which corpus to score (default: the tiered corpora).",
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="Score the fused order directly, to measure what reranking is worth.",
    )
    args = parser.parse_args()
    return asyncio.run(evaluate(args.dataset, rerank=not args.no_rerank))


if __name__ == "__main__":
    raise SystemExit(main())
