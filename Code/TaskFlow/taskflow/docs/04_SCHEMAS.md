# 04 — Schemas

Authoritative definitions for every object that crosses a boundary. Code lives in
`src/taskflow/domain/models.py` and `enums.py` — **if this file and the code disagree,
the code is wrong and must be fixed to match, or this file updated in the same commit.**

All models are Pydantic v2, `frozen=True`, `extra="forbid"`. Frozen because objects flow
through a pipeline where accidental mutation is the most annoying class of bug to find.

## Enums

| Enum | Values |
|---|---|
| `Channel` | email, webchat, console |
| `Intent` | billing, technical, account, feature_request, refund, complaint, general, **unknown** |
| `RouteAction` | auto_send, human_review, template_sent, rejected_spam, dropped_duplicate |
| `GateId` | G1_policy_critical, G2_pii_leak, G3_citations_resolve, G4_intent_allows_auto, G5_classifier_confident, G6_validators_healthy, G7_no_suspicious_context |
| `ReviewState` | pending, approved, edited, rejected, escalated |
| `OutboxState` | queued, sending, sent, failed, dead |
| `Severity` | critical, warning |

`PREEMPT_INTENTS = {complaint, unknown}` — these never reach the drafting stage.

## Core objects

**`InboundMessage`** — the normalized form every channel produces.
`message_id, dedupe_key, tenant_id, channel, sender, subject?, body_text, body_redacted,
thread_ref?, provider_message_id, provider_thread_headers, received_at, raw_ref?`
`dedupe_key = f"{channel}:{provider_message_id}"`. **`body_redacted` is what reaches any
model** — `body_text` is stored for the reviewer only.

**`Chunk`** — `chunk_id, doc_id, title, section?, text, version, source_path, tenant_id`.
`doc_id = sha256(relpath + "\n" + title)[:16]` unless the source document declares its own
in frontmatter, `chunk_id = f"{doc_id}:{ordinal}"`. Deterministic ids make re-ingestion
idempotent — though only an upsert: `ingest_kb.py --recreate` is what clears chunks whose
source no longer produces them.

`text` is what the drafting model reads and cites. It is *not* what was embedded: ingestion
prefixes the title and section heading before encoding, so a chunk carries the context its
heading gave it (`adapters/vector/qdrant_store.embedding_text`).

**`ScoredChunk`** — a `Chunk` plus `rrf_score?, rerank_score?`.
`rrf_score` is a fusion **rank** score: it records where the two retrievers agreed the chunk
sat, and carries no magnitude — its top value is identical whether the best candidate was a
perfect answer or the least bad of twenty irrelevant ones. `rerank_score` is the
cross-encoder's calibrated 0-1 relevance. `final_score` prefers the latter, which is what
lets `min_top_score` in `thresholds.yaml` mean something.

There are no `dense_score` / `sparse_score` fields. Fusion happens inside the vector store
(ADR: avoid pulling two candidate lists into Python), so the per-branch scores never reach
the client and the fields were never populated.

**`RetrievalResult`** — `query_used, chunks[], sufficient, gap_reason?, suspicious_context,
latency_ms, top_score, support_count`.

**`DraftOutput`** — the contract the drafting model must satisfy.
`response_text (1..4000), citations[], tone (formal|friendly|apologetic|neutral|technical),
complexity (simple|moderate|complex), draft_confidence (0..1)`.
Validator: every `citations[].chunk_id` must appear inline as `[chunk_id]` in
`response_text`, or the model rejects the object.
**`draft_confidence` is logged and never read by any routing code.**

**`Citation`** — `chunk_id, doc_title, section?, quote_span?`.

**`ValidatorResult`** — `validator_name, passed, score (0..1), reason, evidence{}, blocking,
latency_ms, errored`. A validator that raises or times out produces
`passed=False, errored=True, reason="validator_error"`, which trips G6.

**`PolicyViolation`** — `rule_id, severity, description, matched_text`.

**`GateResult`** — `gate_id, passed, reason`.

**`ConfidenceBreakdown`** — `gates[], citation_coverage, grounding_entailment,
retrieval_relevance, intent_confidence, tone_alignment, weights{}, score, threshold,
draft_confidence_logged`. Properties: `failed_gates`, `gates_passed`.

**`RoutingDecision`** — `action, reason, reason_code, confidence?, decided_at`.
`reason_code` is the machine-readable one — either a `GateId` value, or one of
`auto_send`, `below_threshold`, `fastpath_hit`, `spam_filter`, `retrieval_gap`,
`all_providers_failed`, `qdrant_unavailable`, `duplicate_dropped`.

**`ReviewItem`** — `review_id, trace_id, conversation_id, tenant_id, state, draft?,
decision, created_at, sla_deadline, escalated_at?`.

**`EditRecord`** — `review_id, original_draft, edited_draft, editor, edited_at, reason?,
char_diff`. Both texts always. This is the training data for the deferred fine-tuning.

**`OutboundMessage`** — `outbound_id, conversation_id, tenant_id, channel, recipient,
subject?, body_text, reply_headers{}, state, idempotency_key, retry_count`.
`idempotency_key = f"{conversation_id}:{sha256(body_text)[:16]}"`.

**`LLMCall`** — `purpose, provider, model, prompt_tokens, completion_tokens, cost_usd,
latency_ms, attempts, repaired, failed_over`.

**`Trace`** — `trace_id, conversation_id, message_id, tenant_id, source_channel,
started_at, finished_at?, intent?, intent_confidence?, classifier_version?, retrieval?,
draft?, validators[], confidence?, decision?, llm_calls[], delivery_result?, errors[]`.
Property `total_cost_usd`.

**`TraceEvent`** — `trace_id, stage, payload{}, elapsed_ms, at`. One row per stage.

## Tables

| Table | Key constraints |
|---|---|
| `inbox` | `PK id`, **`UNIQUE dedupe_key`**, index on `(state, locked_until)` |
| `traces` | `PK trace_id`, index on `(tenant_id, started_at)` |
| `trace_events` | `PK id`, index on `trace_id` |
| `reviews` | `PK review_id`, index on `(state, sla_deadline)` |
| `edit_records` | `PK id`, FK `review_id` |
| `outbox` | `PK outbound_id`, **`UNIQUE idempotency_key`**, index on `(state)` |
| `audit` | `PK id`, append-only, index on `trace_id` |
| `llm_calls` | `PK id`, index on `(trace_id)`, index on `created_at` for the cost tile |
| `alerts` | `PK id` — fallback alert channel and dashboard panel |

Every table has `tenant_id`. Retrofitting multi-tenancy is expensive; a column now is free.

## The `inbox` table in full

```sql
CREATE TABLE inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key   TEXT NOT NULL UNIQUE,     -- deduplication, enforced by the database
    tenant_id    TEXT NOT NULL,
    payload      TEXT NOT NULL,            -- InboundMessage JSON
    state        TEXT NOT NULL DEFAULT 'queued',   -- queued|processing|done|dead
    attempts     INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP,                -- lease: crash recovery
    created_at   TIMESTAMP NOT NULL,
    error        TEXT
);
```

One table, three guarantees: queue, deduplication, retry ledger. See ADR-006.
