# 08 — Observability

The system must answer two questions from a `trace_id` alone, in under 30 seconds,
without reading a log file:

1. **Why did TaskFlow send this response?**
2. **Why was this request escalated?**

If a change makes either question harder to answer, the change is wrong.

## Trace schema

One `traces` row plus N `trace_events` rows per message. Required fields are listed in
`04_SCHEMAS.md`. Every stage writes an event with `stage`, `payload`, `elapsed_ms`.

Stages: `ingest, dedupe, redact, spam, thread_load, fastpath, classify, query_build,
retrieve, rerank, sufficiency, draft, validate, gates, score, route, deliver`.

A schema test asserts that a completed pipeline run produces every expected stage. Trace
completeness is a tested property, not a hope.

**Storage discipline.** Prompts are large: store a prompt hash plus the first 500
characters, with the full text behind a toggle. Otherwise the database grows faster than
the traces are useful.

## Trace viewer (dashboard page 2)

Layout, top to bottom — the answer comes first, evidence after:

```
Trace 01J8X…            email · $0.0021 · 3.4s · provider=claude
ESCALATED — critical policy: refund_ceiling          ← the answer, in the header

▸ 1 Classification    intent=refund (0.93), threshold 0.55, model clf-20260903-1412
                      probability bar chart across all intents
▸ 2 Retrieval         table: chunk_id · title · rrf · rerank
                      sufficient=True, top=0.71, support=3
▸ 3 Generation        the DraftOutput JSON, prompt hash, model, tokens, latency
                      caption: draft_confidence 0.91 — logged only, never routed on
▸ 4 Validation        table: validator · passed · score · reason · latency
▸ 5 Decision          gate list with ✅/❌ and reasons
                      score breakdown: value × weight = contribution, sum, threshold
```

## Review page — the nine required panes

1. original request · 2. retrieved context (expandable, with scores) · 3. citations
(linked to chunk and document) · 4. generated draft (editable) · 5. validator results ·
6. confidence breakdown with per-term contributions · 7. policy flags with rule ids ·
8. thread history · 9. live SLA countdown.

All three action buttons POST to the FastAPI API with the bearer token. **Streamlit never
writes to the database.** The dashboard is a client, not a privileged insider — worth one
sentence on stage.

## Metrics page

- auto-send rate over time, and the escalation-reason histogram (which gate fires most)
- classifier metrics, both sets, with the gap
- retrieval recall and gap-rate
- SLA compliance
- cost: per message, per day, per provider, projected monthly

## Gap analyzer

Every `retrieval_gap` row, clustered by intent, with the query text. This is the page that
answers "what should we write next?" — a genuinely product-useful output from an
engineering system, and a strong closing point.

## Health page

Circuit-breaker states · queue depth and oldest queued age · in-flight leases ·
dead-letter count · worker and SLA-loop heartbeats · Ollama reachable · Claude reachable ·
Gmail token expiry · today's spend against the cap.

## Cost accounting

Every LLM call writes an `llm_calls` row. Cloud cost comes from the token counts times the
prices in `config/providers.yaml`; local calls are recorded at zero cost but with real
latency, so the cost tile also shows *time* spent, which is the real currency in local mode.
