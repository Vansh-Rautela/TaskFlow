#!/usr/bin/env python3
"""Run a single text string through the pipeline and print the result.

Usage:
  uv run python scripts/run_scenario.py --text "I need a refund"
"""

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.repositories import SQLiteTraceRepository
from taskflow.adapters.llm.router import ProviderRouter
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import Deps, run_pipeline


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True, help="The message body text to run")
    args = parser.parse_args()

    print("--- Running scenario ---")
    print(f"Message: {args.text}")

    # 1. Setup Dependencies
    # Connect to the actual SQLite DB initialized in Phase P1
    db_url = "sqlite+aiosqlite:///./data/taskflow.db"
    engine = build_engine(db_url)
    factory = build_session_factory(engine)

    llm_router = ProviderRouter()

    trace_repo = SQLiteTraceRepository(factory)
    deps = Deps(trace_repo=trace_repo, llm_router=llm_router)

    # 2. Inbound Message Construction
    msg_id = uuid.uuid4().hex[:8]
    msg = InboundMessage(
        message_id=msg_id,
        dedupe_key=f"console:{msg_id}",
        tenant_id="taskflow-demo",
        channel=Channel.CONSOLE,
        sender="custom_query_user",
        subject=None,
        body_text=args.text,
        body_redacted=args.text,
        provider_message_id=f"sim-{msg_id}",
        received_at=_utcnow(),
    )

    # 3. Run Pipeline
    try:
        decision = await run_pipeline(msg, deps)

        trace = (await trace_repo.recent(limit=1))[0]

        print("\n=================== PIPELINE EXECUTION RESULT ===================")
        print(f"📌 Message ID:    {msg_id}")
        print(f"🎯 Action Taken:  [{decision.action.value.upper()}]")
        print(f"💡 Reason:        {decision.reason}")
        print(f"🏷️  Reason Code:   {decision.reason_code}")
        print(f"🏷️  Intent:        {trace.intent.value if trace.intent else 'N/A'} (Confidence: {trace.intent_confidence:.2f})")
        if decision.confidence:
            print(f"📊 Final Score:   {decision.confidence.score:.3f} (Threshold: {decision.confidence.threshold:.3f})")
            failed_gates = decision.confidence.failed_gates
            if failed_gates:
                print(f"🛡️  Failed Gates:  {[g.gate_id.value for g in failed_gates]}")
            else:
                print("🛡️  Safety Gates:  All 7 Conjunctive Veto Gates Passed (CLEAR)")

        if trace.draft:
            print("\n🤖 GENERATED DRAFT RESPONSE:")
            print("-" * 75)
            print(trace.draft.response_text)
            print("-" * 75)
            if trace.draft.citations:
                print(f"📚 Citations:     {[f'{c.doc_title} [{c.chunk_id}]' for c in trace.draft.citations]}")

        print(f"💳 LLM Cost:      ${sum(c.cost_usd for c in trace.llm_calls):.6f} USD")
        print(f"🔍 Trace ID:      {trace.trace_id}")
        print("=================================================================\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        return 1
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
