"""Streamlit Human Operator Console & Observability Dashboard for TaskFlow.

Provides interactive human-in-the-loop review operations, trace log exploration,
and real-time LLM performance & cost metrics.
"""

import asyncio
from datetime import UTC, datetime

import streamlit as st

from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.orm import Base
from taskflow.adapters.db.repositories import (
    SQLiteOutboxRepository,
    SQLiteReviewRepository,
    SQLiteTraceRepository,
)
from taskflow.services.review.service import (
    approve_review,
    edit_and_approve_review,
    escalate_review,
    list_pending_reviews,
    reject_review,
)

st.set_page_config(
    page_title="TaskFlow Operator Dashboard",
    page_icon="⚡",
    layout="wide",
)


@st.cache_resource
def _get_factory():
    """Cache DB engine, session factory, and dedicated persistent event loop for Streamlit."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db_url = "sqlite+aiosqlite:///./data/taskflow.db"
    engine = build_engine(db_url)

    async def _init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop.run_until_complete(_init_db())
    factory = build_session_factory(engine)
    return loop, factory


def run_async(coro):
    """Execute coroutine on the persistent cached event loop without closing it."""
    loop, _factory = _get_factory()
    return loop.run_until_complete(coro)


def _get_repos():
    _loop, factory = _get_factory()
    trace_repo = SQLiteTraceRepository(factory)
    review_repo = SQLiteReviewRepository(factory)
    outbox_repo = SQLiteOutboxRepository(factory)
    return trace_repo, review_repo, outbox_repo


st.title("⚡ TaskFlow — Customer Support AI Console")
st.caption("Human-in-the-Loop Review Operations, Governance & Production Observability")

tab1, tab2, tab3 = st.tabs(
    ["📝 Human Review Console", "🔍 Traces & Observability", "📊 System Health & Metrics"]
)

with tab1:
    st.header("Pending Human Review Queue")

    try:
        trace_repo, review_repo, outbox_repo = _get_repos()
        pending_items = run_async(list_pending_reviews(review_repo))
    except Exception as e:
        st.error(f"Failed to connect to database: {e}")
        pending_items = []

    if not pending_items:
        st.success("🎉 Review queue is empty! No messages waiting for human approval.")
    else:
        st.info(f"📋 {len(pending_items)} item(s) requiring human review.")

        item_options = {}
        for item in pending_items:
            sender = item.sender_email or "Unknown Sender"
            reason_code = item.decision.reason_code if item.decision else "review"
            key = f"Review #{item.review_id} | Customer: {sender} | Code: {reason_code} | Trace: {item.trace_id[:8]}"
            item_options[key] = item

        selected_label = st.selectbox("Select Pending Review Item:", list(item_options.keys()))
        selected_item = item_options[selected_label]

        # Top Metadata Summary Bar
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("👤 Customer Email", selected_item.sender_email or "N/A")
        m2.metric("🆔 Review / Trace ID", f"{selected_item.review_id[:10]}... / {selected_item.trace_id[:8]}")
        m3.metric("📌 Reason Code", selected_item.decision.reason_code if selected_item.decision else "N/A")

        score_val = (
            selected_item.decision.confidence.score
            if (selected_item.decision and selected_item.decision.confidence)
            else 0.0
        )
        thresh_val = (
            selected_item.decision.confidence.threshold
            if (selected_item.decision and selected_item.decision.confidence)
            else 0.70
        )
        m4.metric("📊 Score vs Threshold", f"{score_val:.3f} / {thresh_val:.3f}")

        st.divider()

        col1, col2 = st.columns([1, 1])

        with col1:
            st.subheader("📥 Inbound Customer Query")
            inbound_msg = (
                selected_item.inbound_body
                if getattr(selected_item, "inbound_body", None)
                else "No message body found in trace."
            )
            st.info(inbound_msg)

            st.subheader("🧠 Routing & Safety Analysis")
            if selected_item.decision:
                st.markdown(f"**Routing Action:** `{selected_item.decision.action.value.upper()}`")
                st.markdown(f"**Routing Reason:** {selected_item.decision.reason}")
                st.markdown(f"**Reason Code:** `{selected_item.decision.reason_code}`")

                if selected_item.decision.confidence:
                    failed_gates = selected_item.decision.confidence.failed_gates
                    if failed_gates:
                        st.error(f"⚠️ **Failed Veto Gates:** {[g.gate_id.value for g in failed_gates]}")
                    else:
                        st.success("✅ **Safety Gates:** All 7 Veto Gates Passed (CLEAR)")

            if selected_item.draft and selected_item.draft.citations:
                st.subheader("📚 Context Citations & Knowledge Base Chunks")
                for c in selected_item.draft.citations:
                    st.caption(f"• **{c.doc_title}** `[{c.chunk_id}]`")

        with col2:
            st.subheader("✍️ Response Draft Editor")
            original_draft = (
                selected_item.draft.response_text if selected_item.draft else "No draft available"
            )

            edited_text = st.text_area(
                "Review / Edit Outbound Response:",
                value=original_draft,
                height=250,
            )

            st.subheader("💬 Operator Action Reason / Notes")
            operator_reason = st.text_input(
                "Write a Reason / Note for this Action (Optional):",
                value="Actioned via Streamlit Operator Console",
                help="This reason will be logged in the audit trail.",
            )

            btn_col1, btn_col2, btn_col3, btn_col4 = st.columns(4)

            with btn_col1:
                if st.button("✅ Approve", type="primary"):
                    success = run_async(
                        approve_review(
                            review_repo,
                            outbox_repo,
                            selected_item.review_id,
                            operator="streamlit_admin",
                            reason=operator_reason,
                        )
                    )
                    if success:
                        st.success("Approved & queued to outbox!")
                        st.rerun()
                    else:
                        st.error("Failed to approve item.")

            with btn_col2:
                if st.button("✏️ Save & Approve"):
                    success = run_async(
                        edit_and_approve_review(
                            review_repo,
                            outbox_repo,
                            selected_item.review_id,
                            edited_text=edited_text,
                            operator="streamlit_admin",
                            reason=operator_reason,
                        )
                    )
                    if success:
                        st.success("Saved edit & queued to outbox!")
                        st.rerun()
                    else:
                        st.error("Failed to edit item.")

            with btn_col3:
                if st.button("❌ Reject"):
                    success = run_async(
                        reject_review(
                            review_repo,
                            selected_item.review_id,
                            operator="streamlit_admin",
                            reason=operator_reason,
                        )
                    )
                    if success:
                        st.warning("Draft rejected.")
                        st.rerun()
                    else:
                        st.error("Failed to reject item.")

            with btn_col4:
                if st.button("🚀 Escalate"):
                    success = run_async(
                        escalate_review(
                            review_repo,
                            selected_item.review_id,
                            operator="streamlit_admin",
                            reason=operator_reason,
                        )
                    )
                    if success:
                        st.info("Escalated to Tier-2!")
                        st.rerun()
                    else:
                        st.error("Failed to escalate item.")

with tab2:
    st.header("🔍 Trace Explorer & Log Inspection")

    try:
        trace_repo, review_repo, outbox_repo = _get_repos()
        recent_traces = run_async(trace_repo.recent(limit=50))
    except Exception as e:
        st.error(f"Failed to fetch traces: {e}")
        recent_traces = []

    if recent_traces:
        trace_data = []
        for t in recent_traces:
            trace_data.append(
                {
                    "Trace ID": t.trace_id,
                    "Message ID": t.message_id,
                    "Customer": t.source_channel.value,
                    "Started At": t.started_at.strftime("%H:%M:%S") if t.started_at else "",
                    "Intent": t.intent.value if t.intent else "N/A",
                    "Action": t.decision.action.value if t.decision else "N/A",
                    "Reason": t.decision.reason if t.decision else "N/A",
                    "Reason Code": t.decision.reason_code if t.decision else "N/A",
                    "Score": f"{t.confidence.score:.3f}" if t.confidence else "N/A",
                    "LLM Cost ($)": f"${sum(c.cost_usd for c in t.llm_calls):.5f}",
                }
            )

        st.dataframe(trace_data, width="stretch")

        st.subheader("🔎 Trace Detail Inspector")
        trace_map = {f"{t['Trace ID'][:10]}... | {t['Intent']} | {t['Action']}": t for t in trace_data}
        selected_t_key = st.selectbox("Select Trace to Inspect:", list(trace_map.keys()))
        selected_t_summary = trace_map[selected_t_key]

        matching_trace = next((t for t in recent_traces if t.trace_id == selected_t_summary["Trace ID"]), None)
        if matching_trace:
            st.json(matching_trace.model_dump(mode="json"))

    else:
        st.info("No traces recorded yet.")

with tab3:
    st.header("📊 System Health & LLM Metrics")

    try:
        trace_repo, review_repo, outbox_repo = _get_repos()
        traces = run_async(trace_repo.recent(limit=200))
    except Exception as e:
        traces = []

    total_msg = len(traces)
    auto_cnt = sum(
        1 for t in traces if t.decision and t.decision.action.value == "auto_send"
    )
    human_cnt = sum(
        1 for t in traces if t.decision and t.decision.action.value == "human_review"
    )
    total_cost = sum(sum(c.cost_usd for c in t.llm_calls) for t in traces)

    kpi1, kpi2, kpi3, kpi4 = st.columns(4)

    kpi1.metric("Total Messages Processed", f"{total_msg}")
    kpi2.metric(
        "Auto-Send Rate", f"{(auto_cnt / total_msg * 100):.1f}%" if total_msg else "0.0%"
    )
    kpi3.metric(
        "Human Review Rate", f"{(human_cnt / total_msg * 100):.1f}%" if total_msg else "0.0%"
    )
    kpi4.metric("Total LLM Cost ($)", f"${total_cost:.4f}")

    if traces:
        st.subheader("Intent Breakdown")
        intent_counts = {}
        for t in traces:
            ival = t.intent.value if t.intent else "unknown"
            intent_counts[ival] = intent_counts.get(ival, 0) + 1
        st.bar_chart(intent_counts)
