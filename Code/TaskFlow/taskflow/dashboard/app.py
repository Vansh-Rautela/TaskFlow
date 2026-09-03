"""Streamlit Human Operator Console & Observability Dashboard for TaskFlow.

Provides interactive human-in-the-loop review operations, trace log exploration,
and real-time LLM performance & cost metrics.
"""

import asyncio

import streamlit as st

from taskflow.bootstrap import build_runtime
from taskflow.services.review.service import (
    approve_review,
    edit_and_approve_review,
    escalate_review,
    list_pending_reviews,
    reject_review,
)

st.set_page_config(
    page_title="TaskFlow Operator Console",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for polished, clean, professional enterprise design
st.markdown(
    """
    <style>
        .main-header {
            font-size: 1.8rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: #1E293B;
            margin-bottom: 0.1rem;
        }
        .main-subtitle {
            font-size: 0.95rem;
            color: #64748B;
            margin-bottom: 1.5rem;
        }
        div.stButton > button {
            border-radius: 0.375rem;
            font-weight: 500;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.1rem !important;
            word-break: break-word;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def _get_runtime():
    """Build the app once and keep a dedicated event loop for Streamlit's sync world.

    Wiring goes through the shared composition root, so the console reads the same
    database the API writes and Alembic — not create_all — owns the schema.
    The console never retrieves, so the embedding models are not loaded.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop, build_runtime(with_vector_store=False)


def run_async(coro):
    """Execute a coroutine on the persistent cached loop without closing it."""
    loop, _rt = _get_runtime()
    return loop.run_until_complete(coro)


def _get_repos():
    _loop, rt = _get_runtime()
    return rt.trace_repo, rt.review_repo, rt.outbox_repo


def _get_audit_repo():
    _loop, rt = _get_runtime()
    return rt.audit_repo


# Header
st.markdown('<div class="main-header">TaskFlow Operator Console</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="main-subtitle">Human-in-the-Loop Governance & Production Observability Dashboard</div>',
    unsafe_allow_html=True,
)

tab1, tab2, tab3 = st.tabs(["Review Queue", "Trace Explorer", "System Metrics"])

with tab1:
    st.subheader("Pending Human Review Queue")

    try:
        trace_repo, review_repo, outbox_repo = _get_repos()
        pending_items = run_async(list_pending_reviews(review_repo))
    except Exception as e:
        st.error(f"Failed to connect to database: {e}")
        pending_items = []

    if not pending_items:
        st.success("Review queue is empty. No items waiting for human action.")
    else:
        st.info(f"{len(pending_items)} item(s) requiring review.")

        item_options = {}
        for item in pending_items:
            sender = item.sender_email or "Unknown Sender"
            reason_code = item.decision.reason_code if item.decision else "review"
            key = f"Review #{item.review_id} | Customer: {sender} | Code: {reason_code} | Trace: {item.trace_id[:8]}"
            item_options[key] = item

        selected_label = st.selectbox("Select Review Item:", list(item_options.keys()))
        selected_item = item_options[selected_label]

        # Top Metadata Summary Bar
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Customer Email", selected_item.sender_email or "N/A")
        m2.metric(
            "Review / Trace ID",
            f"{selected_item.review_id[:10]}... / {selected_item.trace_id[:8]}",
        )
        m3.metric(
            "Reason Code",
            selected_item.decision.reason_code if selected_item.decision else "N/A",
        )

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
        m4.metric("Score / Threshold", f"{score_val:.3f} / {thresh_val:.3f}")

        st.divider()

        col1, col2 = st.columns([1, 1])

        with col1:
            with st.container(border=True):
                st.subheader("Inbound Customer Query")
                inbound_msg = (
                    selected_item.inbound_body
                    if getattr(selected_item, "inbound_body", None)
                    else "No message body found in trace."
                )
                st.code(inbound_msg, language=None)

            with st.container(border=True):
                st.subheader("Routing & Safety Analysis")
                if selected_item.decision:
                    st.markdown(
                        f"**Routing Action:** `{selected_item.decision.action.value.upper()}`"
                    )
                    st.markdown(f"**Routing Reason:** {selected_item.decision.reason}")
                    st.markdown(f"**Reason Code:** `{selected_item.decision.reason_code}`")

                    if selected_item.decision.confidence:
                        gates = selected_item.decision.confidence.gates
                        failed_gates = selected_item.decision.confidence.failed_gates
                        if failed_gates:
                            failed_str = ", ".join(g.gate_id.value for g in failed_gates)
                            st.error(f"Failed Veto Gates: {failed_str}")
                        elif gates:
                            st.success(f"Safety Gates: All {len(gates)} passed")
                        else:
                            st.warning("Safety Gates: Not recorded for this item")

            if selected_item.draft and selected_item.draft.citations:
                with st.container(border=True):
                    st.subheader("Knowledge Base Citations")
                    for c in selected_item.draft.citations:
                        st.write(f"- **{c.doc_title}** (`{c.chunk_id}`)")

        with col2, st.container(border=True):
            st.subheader("Response Draft Editor")
            original_draft = (
                selected_item.draft.response_text if selected_item.draft else "No draft available"
            )

            edited_text = st.text_area(
                "Review or edit outbound response:",
                value=original_draft,
                height=240,
            )

            st.subheader("Action Audit Notes")
            operator_reason = st.text_input(
                "Reason / Notes (Optional):",
                value="Actioned via Streamlit Operator Console",
                help="Recorded in the system audit log.",
            )

            st.markdown("<br>", unsafe_allow_html=True)
            btn_col1, btn_col2, btn_col3, btn_col4 = st.columns(4)

            with btn_col1:
                if st.button("Approve", type="primary", use_container_width=True):
                    success = run_async(
                        approve_review(
                            review_repo,
                            outbox_repo,
                            selected_item.review_id,
                            operator="streamlit_admin",
                            reason=operator_reason,
                            audit_repo=_get_audit_repo(),
                        )
                    )
                    if success:
                        st.success("Approved and queued to outbox.")
                        st.rerun()
                    else:
                        st.error("Failed to approve item.")

            with btn_col2:
                if st.button("Save & Approve", use_container_width=True):
                    success = run_async(
                        edit_and_approve_review(
                            review_repo,
                            outbox_repo,
                            selected_item.review_id,
                            edited_text=edited_text,
                            operator="streamlit_admin",
                            reason=operator_reason,
                            audit_repo=_get_audit_repo(),
                        )
                    )
                    if success:
                        st.success("Saved edit and queued to outbox.")
                        st.rerun()
                    else:
                        st.error("Failed to edit item.")

            with btn_col3:
                if st.button("Reject", use_container_width=True):
                    success = run_async(
                        reject_review(
                            review_repo,
                            selected_item.review_id,
                            operator="streamlit_admin",
                            reason=operator_reason,
                            audit_repo=_get_audit_repo(),
                        )
                    )
                    if success:
                        st.warning("Draft rejected.")
                        st.rerun()
                    else:
                        st.error("Failed to reject item.")

            with btn_col4:
                if st.button("Escalate", use_container_width=True):
                    success = run_async(
                        escalate_review(
                            review_repo,
                            selected_item.review_id,
                            operator="streamlit_admin",
                            reason=operator_reason,
                            audit_repo=_get_audit_repo(),
                        )
                    )
                    if success:
                        st.info("Escalated to Tier-2 support.")
                        st.rerun()
                    else:
                        st.error("Failed to escalate item.")

with tab2:
    st.subheader("Trace Explorer & Log Inspection")

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
                    "Channel": t.source_channel.value,
                    "Started At": t.started_at.strftime("%H:%M:%S") if t.started_at else "",
                    "Intent": t.intent.value if t.intent else "N/A",
                    "Action": t.decision.action.value if t.decision else "N/A",
                    "Reason": t.decision.reason if t.decision else "N/A",
                    "Reason Code": t.decision.reason_code if t.decision else "N/A",
                    "Score": f"{t.confidence.score:.3f}" if t.confidence else "N/A",
                    "LLM Cost ($)": f"${sum(c.cost_usd for c in t.llm_calls):.5f}",
                }
            )

        st.dataframe(trace_data, use_container_width=True)

        st.subheader("Trace Detail Inspector")
        trace_map = {
            f"{t['Trace ID'][:10]}... | Intent: {t['Intent']} | Action: {t['Action']}": t
            for t in trace_data
        }
        selected_t_key = st.selectbox("Select Trace to Inspect:", list(trace_map.keys()))
        selected_t_summary = trace_map[selected_t_key]

        matching_trace = next(
            (t for t in recent_traces if t.trace_id == selected_t_summary["Trace ID"]), None
        )
        if matching_trace:
            st.json(matching_trace.model_dump(mode="json"))

    else:
        st.info("No traces recorded yet.")

with tab3:
    st.subheader("System Metrics & Performance")

    try:
        trace_repo, review_repo, outbox_repo = _get_repos()
        traces = run_async(trace_repo.recent(limit=200))
    except Exception as err:
        st.error(f"Could not load traces: {err}")
        traces = []

    total_msg = len(traces)
    auto_cnt = sum(1 for t in traces if t.decision and t.decision.action.value == "auto_send")
    human_cnt = sum(1 for t in traces if t.decision and t.decision.action.value == "human_review")
    total_cost = sum(sum(c.cost_usd for c in t.llm_calls) for t in traces)

    kpi1, kpi2, kpi3, kpi4 = st.columns(4)

    kpi1.metric("Total Messages Processed", f"{total_msg}")
    kpi2.metric("Auto-Send Rate", f"{(auto_cnt / total_msg * 100):.1f}%" if total_msg else "0.0%")
    kpi3.metric(
        "Human Review Rate", f"{(human_cnt / total_msg * 100):.1f}%" if total_msg else "0.0%"
    )
    kpi4.metric("Total LLM Cost", f"${total_cost:.4f}")

    if traces:
        st.subheader("Intent Distribution")
        intent_counts: dict[str, int] = {}
        for t in traces:
            ival = t.intent.value if t.intent else "unknown"
            intent_counts[ival] = intent_counts.get(ival, 0) + 1
        st.bar_chart(intent_counts)
