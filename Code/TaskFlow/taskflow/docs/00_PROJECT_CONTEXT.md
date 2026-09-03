# 00 — Project Context

## What this is

**TaskFlow AI Support Agent** — an autonomous email and web-chat support agent with
human-in-the-loop governance, built solo in 4 weeks for a presentation to senior
engineers and leadership.

TaskFlow is a fictional project-management SaaS: Free, Pro at $12/user/month, Enterprise
at $29/user/month. All data is synthetic. One tenant (`taskflow-demo`). English only.

## The thesis

The interesting engineering problem is not "an LLM writes a support reply". It is:

> **A system that knows when it is not allowed to answer, and can prove why.**

Everything in the architecture serves that sentence. The classifier exists so routing is
not decided by a model that can be talked into things. The gates exist so a safety
failure is a veto rather than a number in an average. The trace exists so any decision is
reconstructable months later. The human review queue exists because the correct output
for a hard case is an escalation, not a confident guess.

## What it does, in order

Ingest (Gmail poll, web chat, console) → deduplicate → redact PII → spam filter →
FAQ fast path → classify intent (trained classifier, can abstain) → hybrid retrieval
(dense + BM25 + RRF + rerank) → sufficiency check → draft (Claude, falling back to a
local model) → validators in parallel → **eight conjunctive gates** → weighted score
against a per-intent threshold → auto-send or human review → approve/edit/reject →
send exactly once → trace everything.

## Constraints

| Constraint | Consequence |
|---|---|
| Solo developer, 4 weeks | vertical slices, ruthless scope control, buffer protected |
| Free tier only | embedded Qdrant, SQLite, Ollama, Gmail free API, Teams Workflows webhook |
| Claude API keys are demo-only, temporary | local Ollama fallback is mandatory, not optional |
| Must be explained live, component by component | ~11 components, each with a one-sentence explanation |
| No Docker experience | zero containers; two `uv run` commands |
| Live demo must not fail | offline mode, preflight script, seeded DB, backup recording |

## The five demo scenarios

1. **Exact FAQ** — "How do I reset my password?" → fast path, template, **zero LLM calls**
2. **Billing dispute** — "I was charged twice for Pro" → retrieval, draft, citations, auto-send
3. **Refund over ceiling** — "$750 refund" → **policy gate blocks it at score 0.91**
4. **Spam** — "Win a free iPhone!" → rejected before any model call
5. **Complaint** — "Your product is terrible, I want to talk to someone" → drafting skipped entirely

## Explicitly out of scope

LLM fine-tuning · scheduled retraining (manual script only) · hosted deployment ·
containerization · multi-language · real PII/NER · multi-tenancy beyond `tenant_id`
columns · inbound Teams or Slack bots · load testing.

Each of these appears in `13_PRODUCTION_READINESS_GAP.md` with an effort estimate. They
are deferred decisions, not oversights, and should be presented as such.

## Success criteria

- All five scenarios run correctly, twice in a row, from a reset database
- The demo survives an LLM provider outage (and a full network outage)
- Any decision is explainable from its `trace_id` alone in under 30 seconds
- Zero unverifiable claims in auto-sent responses; zero policy violations auto-sent
- Classifier accuracy measured on a genuinely held-out set **and** a hand-written golden
  set, with the gap between them reported rather than hidden
- Code is typed, layered, and the architectural boundary is enforced by CI
