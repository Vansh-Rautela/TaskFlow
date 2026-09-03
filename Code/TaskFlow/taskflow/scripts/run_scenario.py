#!/usr/bin/env python3
"""Run one message through the real pipeline and print what happened and why.

Uses the same wiring as the API: real database, real retrieval, real providers.
Nothing here is simulated — if no provider is reachable the pipeline takes its
keyword and template fallback paths and says so.

    uv run python scripts/run_scenario.py --text "I need a refund for 3 unused seats"
    uv run python scripts/run_scenario.py --scenario refund_750
    uv run python scripts/run_scenario.py --all
"""

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from taskflow.bootstrap import runtime
from taskflow.config.settings import settings
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import run_pipeline

# data/datasets/demo_scenarios.jsonl is the single source of truth for the named
# scenarios — docs/12_DEMO_SCRIPT.md references the same scenario_ids. Each row also
# carries expected_action/expected_reason_code/demo_note, which this script prints
# but does not assert on; assertions on this exact table live in tests/e2e.
SCENARIOS_PATH = Path(__file__).resolve().parent.parent / "data/datasets/demo_scenarios.jsonl"


def _load_scenarios() -> dict[str, dict]:
    rows = [json.loads(line) for line in SCENARIOS_PATH.read_text().splitlines() if line.strip()]
    return {row["scenario_id"]: row for row in rows}


SCENARIOS: dict[str, dict] = _load_scenarios()


async def run_one(rt, label: str, text: str, demo_note: str | None = None) -> str:
    msg_id = uuid.uuid4().hex[:8]
    msg = InboundMessage(
        message_id=msg_id,
        dedupe_key=InboundMessage.make_dedupe_key(Channel.CONSOLE, msg_id),
        tenant_id=settings().taskflow_tenant_id,
        channel=Channel.CONSOLE,
        sender="scenario-runner@taskflow.local",
        subject=label,
        body_text=text,
        body_redacted=text,
        provider_message_id=f"sim-{msg_id}",
        received_at=datetime.now(UTC),
    )

    decision = await run_pipeline(msg, rt.deps)

    trace = (await rt.trace_repo.recent(limit=1))[0]

    print(f"\n{'=' * 78}")
    print(f" SCENARIO: {label}")
    print(f"{'=' * 78}")
    if demo_note:
        print(f" Proves:       {demo_note}")
    print(f" Message:      {text}")
    print(f" Action:       {decision.action.value.upper()}")
    print(f" Reason:       {decision.reason}")
    print(f" Reason code:  {decision.reason_code}")
    print(
        f" Intent:       {trace.intent.value if trace.intent else 'n/a'} "
        f"(confidence {trace.intent_confidence or 0:.2f})"
    )

    if decision.confidence:
        c = decision.confidence
        print(f" Score:        {c.score:.3f}  (threshold {c.threshold:.3f})")
        print(
            " Signals:      "
            + ", ".join(
                f"{k}={v:.2f}"
                for k, v in {
                    "citations": c.citation_coverage,
                    "grounding": c.grounding_entailment,
                    "retrieval": c.retrieval_relevance,
                    "intent": c.intent_confidence,
                    "tone": c.tone_alignment,
                }.items()
            )
        )
        failed = c.failed_gates
        print(
            f" Gates:        {'ALL PASSED' if not failed else 'FAILED: ' + ', '.join(g.gate_id.value for g in failed)}"
        )

    if trace.retrieval:
        print(
            f" Retrieval:    {len(trace.retrieval.chunks)} chunks, "
            f"top={trace.retrieval.top_score:.3f}, sufficient={trace.retrieval.sufficient}"
        )

    if trace.draft:
        print("\n Draft:")
        for line in trace.draft.response_text.splitlines():
            print(f"   {line}")
        if trace.draft.citations:
            print(f" Citations:    {', '.join(c.chunk_id for c in trace.draft.citations)}")

    print(
        f"\n Cost:         ${sum(c.cost_usd for c in trace.llm_calls):.6f} "
        f"over {len(trace.llm_calls)} LLM call(s)"
    )
    print(f" Trace:        {trace.trace_id}")
    if trace.errors:
        print(f" Errors:       {trace.errors}")

    return decision.action.value


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="an arbitrary message body")
    group.add_argument("--scenario", choices=sorted(SCENARIOS), help="a named scenario")
    group.add_argument("--all", action="store_true", help="run every named scenario")
    args = parser.parse_args()

    async with runtime() as rt:
        if args.all:
            for label, row in SCENARIOS.items():
                await run_one(rt, label, row["text"], row.get("demo_note"))
        elif args.scenario:
            row = SCENARIOS[args.scenario]
            await run_one(rt, args.scenario, row["text"], row.get("demo_note"))
        else:
            await run_one(rt, "ad-hoc", args.text)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
