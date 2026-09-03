# 02 — Architecture Decisions

Format: Context / Options / Decision / Consequences. Never edit an accepted ADR —
supersede it. Any new dependency needs an ADR *before* it enters `pyproject.toml`.

---

## ADR-001 — Safety signals are gates, not weighted score terms

**Status.** Accepted · **Date.** 2026-08-18

**Context.** The original design put `policy_compliance` at weight 0.15 inside a weighted
average, with an auto-send threshold of 0.80 for billing intents. A draft with a critical
policy violation (0.0) and perfect scores elsewhere reaches 0.85 and auto-sends. The PII
validator had no term at all, so it could fail and change nothing. Factual grounding also
had no term despite being the most important validator.

**Options.** (a) raise the policy weight; (b) raise thresholds; (c) split the decision
into conjunctive gates plus a compensatory score.

**Decision.** (c). Seven deterministic gates are evaluated first; any failure routes to
human review regardless of score. The weighted score decides only among drafts that
passed every gate. `thread_coherence` was removed (no honest way to measure it in v1) and
`grounding_entailment` was added.

**Consequences.** Lower auto-send rate, which is the correct direction. Every escalation
has a single reason code. Weights can be tuned freely without ever weakening a safety
property. Test `test_critical_policy_violation_never_auto_sends` is the regression guard
and must never be deleted.

---

## ADR-002 — Claude primary, Ollama local fallback

**Status.** Accepted · **Date.** 2026-08-18

**Context.** No stable OpenAI key. Claude keys are available but only for the demo period.
A support agent that stops working when a key expires is not demonstrable.

**Options.** (a) Claude only; (b) Claude + a second cloud provider; (c) Claude + local
Ollama.

**Decision.** (c). Claude Haiku 4.5 drafts via structured outputs (`output_config.format`,
constrained decoding, GA). Ollama running `qwen2.5:7b-instruct` is the fallback, with the
schema compiled to a GBNF grammar via the `format` parameter — so the fallback has the
same structural guarantee. `TASKFLOW_LLM_MODE=local_only` disables cloud entirely.

**Consequences.** The system runs with no network. Local prose quality is measurably
worse; this is surfaced honestly by showing both drafts side by side, and the gates catch
the difference automatically (weaker drafts fail grounding more often and escalate). The
JSON-repair-and-retry ladder from the earlier design is deleted — both providers
guarantee schema compliance. Requires ~6 GB free RAM; drop to a 3B model if generation
exceeds ~20 s.

---

## ADR-003 — Five validators, one LLM call

**Status.** Accepted · **Date.** 2026-08-18

**Context.** Five independent LLM validators means 5× cost, 5× latency, and five chances
of a malformed response mid-demo — and it is far worse on a local model.

**Options.** (a) five LLM validators; (b) all deterministic; (c) deterministic where the
check is mechanical, LLM only where the check is genuinely semantic.

**Decision.** (c). PII leak, policy compliance, citation validity, tone and completeness
are deterministic. Only factual grounding (per-sentence entailment against the cited
chunk) uses an LLM. Two LLM calls per message total: draft and grounding.

**Consequences.** Cheaper, faster, more explainable, and materially more defensible:
"a non-deterministic guardrail is not a guardrail". Tone scoring is cruder than an LLM
rubric — acceptable, since tone carries the lowest weight and never blocks.

---

## ADR-004 — Configuration over code

**Status.** Accepted · **Date.** 2026-08-18

**Context.** Model names, thresholds and policy rules change during tuning and must be
changeable live on stage.

**Decision.** All of them live in `config/*.yaml` and `config/prompts/`. A pre-commit hook
(`scripts/check_no_hardcoded_models.py`) fails the build if a model id appears in Python.

**Consequences.** Editing `thresholds.yaml` and re-running a scenario flips the routing
decision with no restart — a ten-second live demonstration. Cost: a settings-loading layer
and the discipline to keep using it.

---

## ADR-005 — Explicit async pipeline instead of LangGraph

**Status.** Accepted · **Date.** 2026-08-18

**Context.** The original brief specified LangGraph. The project must be explained
component by component to senior engineers, by one person who wrote all of it.

**Options.** (a) LangGraph with nodes and conditional edges; (b) a plain async function
with explicit branching.

**Decision.** (b). Nine steps of mostly-linear flow with three branch points. LangGraph
would charge a dependency, a state-reducer requirement for concurrent validator writes,
version churn, and a layer of indirection between the author and their own control flow.

**Consequences.** The orchestrator is ~60 lines that can be read aloud. Services remain
pure, so re-introducing LangGraph later is roughly two hours if parallel branches or
mid-graph resumption become real requirements. The answer to "why no orchestration
framework?" is a judgement call with a stated threshold for revisiting it, which is
stronger than compliance with the original brief.

---

## ADR-006 — SQLite table as the queue; no Redis

**Status.** Accepted · **Date.** 2026-08-18

**Context.** The design needs at-least-once delivery, deduplication, retries, and crash
recovery — at a volume of roughly one message every few seconds.

**Options.** (a) Redis Streams with consumer groups; (b) Celery; (c) a leased SQLite table.

**Decision.** (c). One `inbox` table: `UNIQUE(dedupe_key)` provides deduplication,
`locked_until` provides a lease so a crashed worker's message is reclaimed, `attempts`
provides the retry ledger. Claim is a single `UPDATE ... RETURNING`.

**Consequences.** One fewer dependency and one fewer server. Same delivery semantics at
this scale. Will not scale to multiple worker processes with high contention — at that
point the `EventBus` port is swapped for a Redis Streams adapter, touching one file.

---

## ADR-007 — Embedded Qdrant; no Docker

**Status.** Accepted · **Date.** 2026-08-18

**Context.** The developer has no Docker experience and a 4-week deadline. The corpus is
42 documents, roughly 200 chunks.

**Options.** (a) Qdrant in Docker; (b) numpy brute force + `rank_bm25`; (c) embedded
Qdrant via `QdrantClient(path=...)`.

**Decision.** (c). Qdrant's local mode is a supported in-process implementation intended
for development, testing and demos up to roughly 20,000 points, and it implements sparse
vectors, `query_points` with prefetch, and RRF fusion. The retrieval code is identical to
the server version.

**Consequences.** Zero containers, zero servers, `uv run` is the whole runtime. Migrating
to a Qdrant server is a one-line client change. Docker moves to
`13_PRODUCTION_READINESS_GAP.md` as a deliberate next step rather than a half-understood
dependency on stage.

---

## ADR-008 — Teams for alerts only; web chat for customer conversations

**Status.** Accepted · **Date.** 2026-08-18

**Context.** Microsoft permanently disabled Office 365 connector webhooks in Teams during
18–22 May 2026; the original brief's "Teams incoming webhook, ~30 minutes" no longer
exists. Inbound Teams bots require Azure Bot Service registration, a public HTTPS
endpoint, and tenant-admin app sideloading permission. Slack was considered and is
unavailable under the free-tier constraint.

**Decision.** Outbound alerts go through a Power Automate **Workflows** webhook, behind an
`AlertChannel` port with a dashboard-plus-email fallback when Power Automate access is
absent. Customer chat is a small web page served by our own FastAPI app.

**Consequences.** No external dependency can block the demo, and the chat is visible on
the same screen as everything else. Messages posted to Teams appear under the default Flow
bot identity, which cannot be customised. Adding Teams or Slack as an inbound channel
later is one more `ChannelConnector` implementation, not a re-architecture — this is the
answer to give when asked why there is no "real" chat platform.

---

## ADR-009 — <next decision>

Add ADRs as you go. Anything that took more than five minutes to decide belongs here.
