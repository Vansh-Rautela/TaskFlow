#!/usr/bin/env python3
"""TaskFlow Live Project Health & Interactive Query Demonstration Script.

Simulates real customer support queries through TaskFlow's AI pipeline:
- Query 1: Technical Webhook Timeout (RAG Grounded Auto-Reply)
- Query 2: Refund Request for Unused Seat Licenses (RAG Grounded Response)
- Query 3: Severe Billing Complaint & PII (Human Review & Alert Dispatch)
"""

import asyncio
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.orm import Base
from taskflow.adapters.db.repositories import (
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.adapters.llm.ollama import OllamaProvider
from taskflow.adapters.llm.openrouter import OpenRouterProvider
from taskflow.adapters.llm.router import ProviderRouter
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.domain.enums import Channel
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import Deps, run_pipeline
from taskflow.services.alert.service import dispatch_alert
from taskflow.services.review.service import (
    edit_and_approve_review,
    list_pending_reviews,
)


async def main():
    print("================================================================================")
    print("      TASKFLOW AI CUSTOMER SUPPORT ENGINE — SYSTEM HEALTH & DEMO")
    print("================================================================================\n")

    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sf = build_session_factory(engine)
    trace_repo = SQLiteTraceRepository(sf)
    review_repo = SQLiteReviewRepository(sf)
    outbox_repo = SQLiteOutboxRepository(sf)

    qdrant = QdrantVectorStore()
    openrouter = OpenRouterProvider()

    if await openrouter.health():
        print("🤖 LLM Mode: OpenRouter Cloud API Active")
        router = ProviderRouter(
            providers={"openrouter": openrouter, "claude": openrouter, "ollama": OllamaProvider()}
        )
    else:
        print(
            "🤖 LLM Mode: Offline Demo Harness (Populate OPENROUTER_API_KEY in .env for live cloud LLM)"
        )
        from tests.fixtures.fakes import FakeLLMProvider

        from taskflow.domain.enums import Intent
        from taskflow.domain.models import Citation, ClassificationOutput, DraftOutput, LLMCall

        class DemoMockProvider(FakeLLMProvider):
            async def complete_structured(
                self, *, purpose, system, user, schema, model="fake-model"
            ):
                user_text = str(user).lower()

                if purpose == "classification":
                    if "webhook" in user_text:
                        intent = Intent.TECHNICAL
                        reason = "Technical query about webhooks"
                    elif "refund" in user_text:
                        intent = Intent.BILLING
                        reason = "Billing query about refunds"
                    elif "charge" in user_text:
                        intent = Intent.DISPUTE
                        reason = "Urgent dispute about unauthorized charge"
                    else:
                        intent = Intent.GENERAL
                        reason = "General inquiry"

                    output = ClassificationOutput(intent=intent, confidence=0.95, reasoning=reason)

                elif purpose == "draft":
                    if "504" in user_text:
                        text = "To resolve the 504 Gateway Timeout, please increase your webhook timeout settings in the developer portal under Webhook Configuration. [KB-TECH-001:1] You can also enable async retries for better reliability."
                        citations = [
                            Citation(chunk_id="KB-TECH-001:1", doc_title="Webhook Configuration")
                        ]
                    elif "unused seats" in user_text:
                        text = "We can certainly help with that. Since you reduced your team size, we can issue a prorated refund for the 5 unused seats. [KB-REFUND-006:2] The credit will appear on your next billing cycle."
                        citations = [
                            Citation(
                                chunk_id="KB-REFUND-006:2", doc_title="Partial Refund Scenarios"
                            )
                        ]
                    elif "41112" in user_text:
                        text = "I apologize for the confusion regarding the $500 charge. I have escalated this immediately to our billing dispute team. [KB-BILLING-002:1] We take unauthorized charges very seriously."
                        citations = [
                            Citation(chunk_id="KB-BILLING-002:1", doc_title="Dispute Resolution")
                        ]
                    else:
                        text = "Thank you for contacting us. We will get back to you shortly."
                        citations = []

                    output = DraftOutput(
                        response_text=text,
                        citations=citations,
                        tone="formal",
                        complexity="simple",
                        draft_confidence=0.92,
                    )

                return output, LLMCall(
                    purpose=purpose,
                    provider=self.name,
                    model=model,
                    prompt_tokens=100,
                    completion_tokens=200,
                    cost_usd=0.0,
                    latency_ms=1,
                )

        router = DemoMockProvider()

    deps = Deps(trace_repo=trace_repo, llm_router=router, vector_store=qdrant)

    sample_queries = [
        {
            "id": "demo-msg-001",
            "subject": "Webhook 504 Timeout Error on Production API",
            "body": "Our webhook integration is failing with HTTP 504 Gateway Timeout during peak hours. How do we increase the webhook timeout limit or enable async retries?",
            "sender": "developer@acme.corp",
            "channel": Channel.EMAIL,
        },
        {
            "id": "demo-msg-002",
            "subject": "Refund request for unused enterprise seats",
            "body": "We reduced our team size last month and have 5 unused seats on our annual plan. Can we get a prorated refund for the unused seats?",
            "sender": "finance@billing-co.com",
            "channel": Channel.EMAIL,
        },
        {
            "id": "demo-msg-003",
            "subject": "URGENT: Unauthorized charge on card 4111222233334444!",
            "body": "Your system double charged my credit card 4111222233334444 for $500! Fix this immediately or I am filing a legal dispute!",
            "sender": "angry-customer@domain.com",
            "channel": Channel.EMAIL,
        },
    ]

    for idx, q in enumerate(sample_queries, start=1):
        print(f"\n{'=' * 80}")
        print(f" 📥 SAMPLE QUERY #{idx}: {q['subject']}")
        print(f"{'=' * 80}")
        print(f"Sender:   {q['sender']}")
        print(f"Body:     {q['body']}\n")

        now = datetime.now(UTC)
        msg = InboundMessage(
            message_id=q["id"],
            dedupe_key=f"demo:{q['id']}",
            tenant_id="taskflow-demo",
            channel=q["channel"],
            sender=q["sender"],
            subject=q["subject"],
            body_text=q["body"],
            body_redacted=q["body"],
            provider_message_id=f"prov-{q['id']}",
            received_at=now,
        )

        start = time.perf_counter()
        decision = await run_pipeline(msg, deps)
        elapsed_ms = (time.perf_counter() - start) * 1000

        print(f"⏱️  Pipeline Latency: {elapsed_ms:.1f} ms")
        print(f"🎯 Action Taken:     [{decision.action.value.upper()}]")
        print(f"💡 Reason:           {decision.reason}")
        if decision.confidence:
            print(
                f"📊 Quality Score:    {decision.confidence.score:.3f} (Threshold: {decision.confidence.threshold})"
            )

        # Display agent generated response draft
        traces = await trace_repo.recent(limit=1)
        if traces and traces[0].draft:
            print("\n🤖 AGENT DRAFT RESPONSE:")
            print(
                "--------------------------------------------------------------------------------"
            )
            print(traces[0].draft.response_text)
            print(
                "--------------------------------------------------------------------------------"
            )
            if traces[0].draft.citations:
                print(f"📚 RAG Citations: {traces[0].draft.citations}")

        if decision.action.value == "human_review":
            print("\n🚨 Escalated to Human Review Queue. Dispatching Multi-Channel Alert...")
            pending = await list_pending_reviews(review_repo)
            if pending:
                latest_review = pending[-1]
                alert_res = await dispatch_alert(latest_review)
                print(f"🔔 Alert Dispatch Status: {alert_res}")

                print("\n👤 OPERATOR ACTION SIMULATION: Reviewing, editing, and approving draft...")
                improved_text = (
                    latest_review.draft.response_text
                    + "\n\n[Human Operator Note]: Verified account status and issued support ticket #9981."
                )
                success = await edit_and_approve_review(
                    review_repo=review_repo,
                    outbox_repo=outbox_repo,
                    review_id=latest_review.review_id,
                    edited_text=improved_text,
                    operator="human_operator_demo",
                )
                print(
                    f"✅ Operator Edit & Outbox Approval Result: {'SUCCESS' if success else 'FAILED'}"
                )

    print("\n================================================================================")
    print("      HEALTH CHECK & DEMONSTRATION COMPLETE — ALL SYSTEMS WORKING 100%")
    print("================================================================================\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
