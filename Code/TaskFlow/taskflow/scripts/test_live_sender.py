#!/usr/bin/env python3
"""Live End-to-End Test Script: Sender Response & Telegram Alerts.

Simulates an inbound support email from vaannsshhh@gmail.com:
1. Runs the full AI RAG pipeline to generate a grounded response draft.
2. If AUTO_SEND: Automatically delivers the reply email to vaannsshhh@gmail.com via Gmail SMTP.
3. If HUMAN_REVIEW: Sends detailed Telegram alert + email notification, then simulates operator review and sends the final reply email to vaannsshhh@gmail.com!
"""

import asyncio
import sys
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
from taskflow.services.review.service import edit_and_approve_review, list_pending_reviews
from taskflow.worker.outbox_worker import process_outbox_batch


async def run_live_test(sender_email: str = "vaannsshhh@gmail.com"):
    print("================================================================================")
    print(f"      LIVE SENDER RESPONSE & TELEGRAM ALERT TEST -> {sender_email}")
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
        print("🤖 Provider: OpenRouter Cloud LLM Active")
        router = ProviderRouter(
            providers={"openrouter": openrouter, "claude": openrouter, "ollama": OllamaProvider()}
        )
    else:
        print("🤖 Provider: Offline Fallback LLM Active")
        from tests.fixtures.fakes import FakeLLMProvider

        from taskflow.domain.enums import Intent
        from taskflow.domain.models import Citation, ClassificationOutput, DraftOutput

        router = FakeLLMProvider(
            responses={
                "classification": ClassificationOutput(
                    intent=Intent.REFUND, confidence=0.95, reasoning="Prorated refund request"
                ),
                "draft": DraftOutput(
                    response_text="Hi Vansh,\n\nThank you for contacting TaskFlow Support. [KB-REFUND-006:2]\n\nPer our subscription terms, unused seat licenses are credited on your next renewal billing cycle. We have issued a manual adjustment for your account.\n\nBest regards,\nTaskFlow Support AI",
                    citations=[
                        Citation(chunk_id="KB-REFUND-006:2", doc_title="Partial Refund Scenarios")
                    ],
                    tone="friendly",
                    complexity="simple",
                    draft_confidence=0.95,
                ),
            }
        )

    deps = Deps(
        trace_repo=trace_repo,
        llm_router=router,
        vector_store=qdrant,
        review_repo=review_repo,
        outbox_repo=outbox_repo,
    )

    sample_queries = [
        {
            "subject": "Question regarding prorated refund for enterprise seats",
            "body": "Hi TaskFlow Team, we reduced our team size and need a prorated refund for 5 unused seats. Please help.",
        },
        {
            "subject": "URGENT: Billing dispute & charge error",
            "body": "Your system double charged my credit card 4111222233334444 for $500! Please fix this immediately!",
        },
        {
            "subject": "LEGAL THREAT: My business is ruined because of you!",
            "body": "I am furious! Your recent outage cost me $10,000 in lost sales. I am filing a lawsuit against you tomorrow unless a manager calls me immediately. This is completely unacceptable!",
        },
    ]

    for idx, q in enumerate(sample_queries, start=1):
        print(f"\n{'=' * 80}")
        print(f" 📥 INBOUND EMAIL #{idx} FROM {sender_email}")
        print(f"   Subject: {q['subject']}")
        print(f"   Body:    {q['body']}")
        print(f"{'=' * 80}")

        msg = InboundMessage(
            message_id=f"live-msg-{idx}",
            dedupe_key=f"live:{idx}:{sender_email}",
            tenant_id="taskflow-demo",
            channel=Channel.EMAIL,
            sender=sender_email,
            subject=q["subject"],
            body_text=q["body"],
            body_redacted=q["body"],
            provider_message_id=f"prov-{idx}",
            received_at=datetime.now(UTC),
        )

        decision = await run_pipeline(msg, deps)
        print(f"\n🎯 Pipeline Decision: [{decision.action.value.upper()}] — {decision.reason}")

        if decision.action.value == "auto_send":
            print(f"✉️  Auto-sending reply directly to {sender_email}...")
            sent_count = await process_outbox_batch(outbox_repo)
            print(f"✅ Outbox Delivery Processed: {sent_count} email sent to {sender_email}")

        elif decision.action.value == "human_review":
            print("🚨 Flagged for Human Review. Sending Telegram & Email Alerts...")
            pending = await list_pending_reviews(review_repo)
            if pending:
                latest = pending[-1]
                alert_res = await dispatch_alert(latest)
                print(f"📱 Telegram & Email Alert Status: {alert_res}")

                print("\n👤 Simulating Operator Editing & Approving Draft...")
                approved_text = (
                    latest.draft.response_text
                    + f"\n\n[Human Operator Note]: Case verified. Support ticket #990{idx} opened for {sender_email}."
                )
                await edit_and_approve_review(
                    review_repo=review_repo,
                    outbox_repo=outbox_repo,
                    review_id=latest.review_id,
                    edited_text=approved_text,
                    operator="operator_admin",
                )
                print(f"✉️  Delivering approved reply email to {sender_email}...")
                sent_count = await process_outbox_batch(outbox_repo)
                print(f"✅ Outbox Delivery Processed: {sent_count} email sent to {sender_email}")

    print("\n================================================================================")
    print("      LIVE TEST COMPLETE — REPLIES SENT TO SENDER & TELEGRAM ALERTS SENT")
    print("================================================================================\n")

    await engine.dispose()


if __name__ == "__main__":
    sender = sys.argv[1] if len(sys.argv) > 1 else "vaannsshhh@gmail.com"
    asyncio.run(run_live_test(sender))
