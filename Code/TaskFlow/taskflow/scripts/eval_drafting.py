#!/usr/bin/env python3
"""Drafting and routing evaluation over the real pipeline.

Runs golden-set queries through the actual pipeline and reports where they land:
the action distribution, which gates fired, the signal averages, and cost.

This makes real LLM calls. The previous version imported `MockSuccessProvider` from
the test suite, so every "draft" it evaluated was the same canned string — the
numbers it published described a constant, not the system.

    uv run python scripts/eval_drafting.py --limit 10
    uv run python scripts/eval_drafting.py --limit 10 --offline   # local Ollama only
"""

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from taskflow.bootstrap import runtime
from taskflow.config.settings import settings
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import run_pipeline

GOLDEN_EVAL_PATH = Path("data/datasets/golden_eval.jsonl")
METRICS_OUT_PATH = Path("docs/metrics/drafting.md")


def _load_rows(limit: int | None) -> list[dict]:
    if not GOLDEN_EVAL_PATH.exists():
        return []
    rows = [
        json.loads(line)
        for line in GOLDEN_EVAL_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return rows[:limit] if limit else rows


async def evaluate(limit: int | None) -> int:
    rows = _load_rows(limit)
    if not rows:
        print(f"Error: {GOLDEN_EVAL_PATH} not found or empty. Run scripts/generate_datasets.py.")
        return 1

    print(f"Running {len(rows)} golden queries through the real pipeline.")
    print(f"LLM mode: {settings().taskflow_llm_mode}. This makes real provider calls.\n")

    actions: Counter[str] = Counter()
    reason_codes: Counter[str] = Counter()
    failed_gates: Counter[str] = Counter()
    signals: dict[str, list[float]] = {
        "citation_coverage": [],
        "grounding_entailment": [],
        "retrieval_relevance": [],
        "intent_confidence": [],
        "tone_alignment": [],
    }
    scores: list[float] = []
    latencies: list[float] = []
    total_cost = 0.0
    fallback_drafts = 0

    async with runtime() as rt:
        for i, row in enumerate(rows, start=1):
            msg_id = f"eval-{uuid.uuid4().hex[:10]}"
            msg = InboundMessage(
                message_id=msg_id,
                dedupe_key=InboundMessage.make_dedupe_key(Channel.CONSOLE, msg_id),
                tenant_id=settings().taskflow_tenant_id,
                channel=Channel.CONSOLE,
                sender="eval@taskflow.local",
                subject="eval",
                body_text=row.get("text", ""),
                body_redacted=row.get("text", ""),
                provider_message_id=msg_id,
                received_at=datetime.now(UTC),
            )

            start = time.perf_counter()
            decision = await run_pipeline(msg, rt.deps)
            latencies.append((time.perf_counter() - start) * 1000)

            actions[decision.action.value] += 1
            reason_codes[decision.reason_code] += 1

            if decision.confidence:
                c = decision.confidence
                scores.append(c.score)
                signals["citation_coverage"].append(c.citation_coverage)
                signals["grounding_entailment"].append(c.grounding_entailment)
                signals["retrieval_relevance"].append(c.retrieval_relevance)
                signals["intent_confidence"].append(c.intent_confidence)
                signals["tone_alignment"].append(c.tone_alignment)
                for gate in c.failed_gates:
                    failed_gates[gate.gate_id.value] += 1

            trace = (await rt.trace_repo.recent(limit=1))[0]
            total_cost += sum(call.cost_usd for call in trace.llm_calls)
            if not any(
                call.purpose in ("draft", "completion", "complete") for call in trace.llm_calls
            ):
                fallback_drafts += 1

            print(f"  [{i}/{len(rows)}] {decision.action.value:<18} {decision.reason_code}")

    n = len(rows)

    def mean(values: list[float]) -> float:
        return statistics.mean(values) if values else 0.0

    print("\n=== ROUTING DISTRIBUTION ===")
    for action, count in actions.most_common():
        print(f"  {action:<20} {count:>3}  ({count / n * 100:.1f}%)")

    print("\n=== REASON CODES ===")
    for code, count in reason_codes.most_common():
        print(f"  {code:<28} {count}")

    if failed_gates:
        print("\n=== GATE FAILURES ===")
        for gate, count in failed_gates.most_common():
            print(f"  {gate:<28} {count}")

    print("\n=== SIGNAL AVERAGES ===")
    for name, values in signals.items():
        spread = (max(values) - min(values)) if values else 0.0
        note = "  <-- effectively constant" if values and spread < 0.01 else ""
        print(f"  {name:<24} {mean(values):.3f}  (spread {spread:.3f}){note}")

    print(f"\n  mean score        {mean(scores):.3f}")
    print(f"  mean latency      {mean(latencies):.0f} ms")
    print(f"  template drafts   {fallback_drafts}/{n}")
    print(f"  total cost        ${total_cost:.6f}")

    METRICS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Drafting & Routing Metrics",
        "",
        "Generated by `scripts/eval_drafting.py` against the **real** pipeline —",
        "real retrieval, real providers, real gates. No canned responses.",
        "",
        f"- Queries: {n}",
        f"- LLM mode: `{settings().taskflow_llm_mode}`",
        f"- Mean score: {mean(scores):.3f}",
        f"- Mean latency: {mean(latencies):.0f} ms",
        f"- Template (non-model) drafts: {fallback_drafts}/{n}",
        f"- Total cost: ${total_cost:.6f}",
        "",
        "## Routing distribution",
        "",
        "| Action | Count | Share |",
        "|---|---|---|",
    ]
    lines += [f"| `{a}` | {c} | {c / n * 100:.1f}% |" for a, c in actions.most_common()]
    lines += ["", "## Reason codes", "", "| Reason code | Count |", "|---|---|"]
    lines += [f"| `{c}` | {n_} |" for c, n_ in reason_codes.most_common()]
    if failed_gates:
        lines += ["", "## Gate failures", "", "| Gate | Times fired |", "|---|---|"]
        lines += [f"| `{g}` | {c} |" for g, c in failed_gates.most_common()]
    lines += [
        "",
        "## Signal averages",
        "",
        "A signal whose spread is near zero contributes a constant to every decision and",
        "is not informing routing, whatever its weight.",
        "",
        "| Signal | Mean | Spread |",
        "|---|---|---|",
    ]
    for name, values in signals.items():
        spread = (max(values) - min(values)) if values else 0.0
        lines.append(f"| `{name}` | {mean(values):.3f} | {spread:.3f} |")
    await asyncio.to_thread(METRICS_OUT_PATH.write_text, "\n".join(lines) + "\n", "utf-8")
    print(f"\nPublished report to {METRICS_OUT_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="number of queries (0 = all)")
    parser.add_argument("--offline", action="store_true", help="force local_only mode")
    args = parser.parse_args()

    if args.offline:
        os.environ["TASKFLOW_LLM_MODE"] = "local_only"
        from taskflow.config import settings as settings_module

        settings_module.settings.cache_clear()

    return asyncio.run(evaluate(args.limit or None))


if __name__ == "__main__":
    raise SystemExit(main())
