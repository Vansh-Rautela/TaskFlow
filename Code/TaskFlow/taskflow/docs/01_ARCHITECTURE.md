# 01 — Architecture

## Runtime: two processes, zero servers

```
uv run uvicorn taskflow.api.app:app --port 8000      # API (workers and pollers are separate processes: make worker / poller / sla)
uv run streamlit run src/taskflow/dashboard/app.py   # review queue + trace viewer
```

| Thing | Where it lives | Notes |
|---|---|---|
| FastAPI | :8000 | `/chat` widget, `/api/reviews/*`, `/api/messages`, `/health` |
| Worker loop | asyncio task inside the API process | claims from `inbox`, runs the pipeline |
| SLA loop | asyncio task, 30s tick | escalates overdue reviews |
| Gmail poller | asyncio task, 30s tick | `history_id` cursor persisted |
| Outbox dispatcher | asyncio task, 5s tick | send-once delivery |
| Streamlit | :8501 | reads SQLite read-only; **all writes go through the API** |
| Qdrant | `./data/qdrant/` | embedded, file-backed, no process |
| SQLite | `./data/taskflow.db` | WAL mode |
| Ollama | :11434 | a separate app you start once |

The worker runs in the API process because at one message every few seconds that is the
correct amount of infrastructure. `worker_loop()` is a standalone function with its own
entry point, so splitting it out is a deployment change, not a code change.

## Layers

```
api/  dashboard/          delivery mechanisms — no business logic
        |
pipeline/                 explicit async orchestrator (~60 lines)
        |
services/                 ALL business logic — pure, ports injected
        |
ports/                    Protocol definitions — the seams
        |
adapters/                 the only place vendor SDKs appear
        |
domain/                   Pydantic models, enums, errors — imported by everything, imports nothing
```

`domain/` and `services/` may not import `adapters/`, `api/`, or any SDK. This is enforced
by `lint-imports` in `make typecheck`, not by discipline. It is what makes
"SQLite → Postgres is a config change" a true statement.

## The pipeline

```
inbox row
  → PII redaction (regex)
  → spam filter (rules)          → reject
  → thread/state load
  → FAQ fast path                → template reply, 0 LLM calls
  → classify intent              → complaint/unknown/abstain → human review
  → build query (deterministic: normalise + alias expansion)
  → Qdrant: dense + BM25 → RRF   (one call, top 20)
  → cross-encoder rerank → top 5 (scored on the customer's raw words)
  → sufficiency check            → insufficient → log gap → human review
  → draft                        (LLM call 1: Claude → Ollama)
  → validators, asyncio.gather   (4 deterministic + LLM call 2: grounding)
  → GATES G1-G7                  → any failure → human review
  → weighted score vs per-intent threshold → below → human review
  → auto-send
```

## The decision, in two layers

This is the core of the project and the thing to explain most carefully.

**Layer 1 — gates (conjunctive, veto-only):**

| Gate | Fails when | Deterministic? |
|---|---|---|
| G1 policy | a critical rule in `policies.yaml` matches the draft | yes |
| G2 PII | a PII pattern appears in outbound text | yes |
| G3 citations | a cited `chunk_id` was not retrieved in this run | yes |
| G4 intent | intent is complaint or unknown | yes |
| G5 abstain | classifier max probability below `abstain_threshold` | yes |
| G6 validators | any validator errored or timed out | yes |
| G7 injection | instruction-like text found in retrieved context | yes |

All seven are deterministic. That is deliberate: a guardrail that depends on a model's
judgement is not a guardrail.

**Layer 2 — score (compensatory, quality only), reached only if all gates pass:**

```
score = citation_coverage    * 0.35
      + grounding_entailment * 0.25
      + retrieval_relevance  * 0.20
      + intent_confidence    * 0.10
      + tone_alignment       * 0.10
```

Compared against a per-intent threshold from `config/thresholds.yaml`
(general 0.70 … billing 0.80 … refund 0.90 … complaint unreachable).

**Why it is split this way:** safety properties are conjunctive, quality properties are
compensatory. Averaging a veto into a quality score is a category error, and produces the
concrete bug where a draft that violates a critical policy but scores well everywhere else
clears the threshold and auto-sends. See ADR-001.

## Storage

One SQLite file, WAL mode, single writer (the API process), read-only readers (Streamlit).

| Table | Purpose |
|---|---|
| `inbox` | **queue + deduplication + retry ledger in one table** — `UNIQUE(dedupe_key)` is the dedupe, `locked_until` is the crash-recovery lease |
| `traces`, `trace_events` | full decision record per message |
| `reviews`, `edit_records` | human loop; both draft texts kept on every edit |
| `outbox` | send-once delivery, `UNIQUE(idempotency_key)` |
| `audit` | append-only record of every action |
| `llm_calls` | cost and latency per call |
| `alerts` | email, Telegram and generic-webhook dispatch; each enabled by its own credential. Teams was never implemented (ADR-008) |

Vectors live in embedded Qdrant, not SQLite: one collection, named vectors `dense` (384d
cosine) and `bm25` (sparse, IDF modifier), payload-indexed on `tenant_id`, `doc_type`,
`product_tier`, `intents`, `doc_id`.

## Providers

`ProviderRouter` walks `config/providers.yaml` priority order. Both providers guarantee
schema compliance through constrained decoding, so there is no JSON-repair ladder:

- **Claude** — `messages.parse(output_format=DraftOutput)`; raises `SchemaError` on a
  refusal or a max_tokens stop reason
- **Ollama** — `format=DraftOutput.model_json_schema()`, compiled to a GBNF grammar

If every provider fails, the message routes to human review with
`reason_code=all_providers_failed`. Set `TASKFLOW_LLM_MODE=local_only` and the whole
system runs with no network at all.
