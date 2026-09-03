# 03 — Implementation Plan

**Eleven phases, four weeks.** Each phase is independently verifiable and leaves the
system demoable. Tick the checkboxes as you go — this file is the source of truth for
"what's next", and the `/start-phase` workflow reads it.

**How to work a phase:** read it → produce a plan of files and interfaces → get it
approved → implement one numbered step at a time → run `make check` between steps →
run the `verify-slice` skill → commit as `P{n}: <what changed>`.

**Rule:** if a phase runs more than 1.5× its estimate, stop and cut from
§Scope cuts at the bottom of this file. Never borrow from the week-4 buffer.

---

## P-1 — Accounts and credentials (do this before Day 1)

Not code. It is the classic solo time sink because every step has a waiting period.

- [ ] Google Cloud project `taskflow-demo`, Gmail API enabled
- [ ] OAuth consent screen (External, Testing), your demo Gmail added as a **test user**
- [ ] OAuth client ID (Desktop app) downloaded to `secrets/credentials.json`
- [ ] A dedicated demo Gmail account created — **not** your personal inbox
- [ ] Anthropic API key in `.env`, verified with one curl
- [ ] Ollama installed, `ollama pull qwen2.5:7b-instruct`, `ollama run` smoke-tested
- [ ] Teams: channel → ⋯ → Workflows → "Post to a channel when a webhook request is
      received" → URL in `.env`. **If you have no Power Automate access, skip it** —
      the fallback alerter needs no setup.
- [ ] Measured: how long does your machine take to generate 300 words locally?
      Record it. If over 20 s, plan to demo on `qwen2.5:3b-instruct`.

> Gmail refresh tokens expire after 7 days while the consent screen is in Testing mode.
> `make preflight` checks token validity and warns under 2 days. Re-auth the day before.

---

## P0 — Skeleton and quality gates · ~0.5 day

**Goal.** A repository that lints, type-checks, tests, and enforces its own architecture.
**Prerequisites.** None.
**Reading.** `docs/01_ARCHITECTURE.md`, `docs/02_ARCHITECTURE_DECISIONS.md`.

**Files.** `pyproject.toml`, `Makefile`, `.env`, `.gitignore`, `.pre-commit-config.yaml`,
`src/taskflow/**/__init__.py`, `tests/`, `scripts/check_no_hardcoded_models.py`.

**Interfaces.** None.

**Steps.**
1. `uv sync`; confirm the package imports.
2. Confirm `ruff`, `mypy`, and `lint-imports` all run clean on the empty tree.
3. Write `scripts/check_no_hardcoded_models.py`: greps `src/` for `claude-`, `qwen`,
   `gpt-`, `llama` and exits 1 on a hit outside `config/`.
4. `pre-commit install`.
5. Write `tests/unit/test_imports.py` so the harness is proven to run.

**Tests.** `test_imports.py`; `test_no_hardcoded_models` calling the script.
**Verification.** `make check` green from a clean clone.
**Definition of done.**
- [ ] `make check` passes
- [ ] `lint-imports` reports the contract as satisfied
- [ ] `uv sync` reproduces from `uv.lock`

**Failure modes.** `import-linter` config path wrong → it silently reports zero contracts;
assert the output mentions "1 contract". Python 3.13 in the venv → some ML wheels missing;
pin 3.12.

---

## P1 — Domain, ports, persistence · ~1 day

**Goal.** Every object that crosses a boundary is typed, and there is exactly one way to
persist it. **Freeze these before writing services** — six modules will consume them.

**Prerequisites.** P0.
**Reading.** `04_SCHEMAS.md` (authoritative), `01_ARCHITECTURE.md` §Storage.

**Files.** `domain/{models,enums,errors}.py` (models and enums are already written —
review, don't rewrite), `ports/*.py` (already written), `adapters/db/{engine,orm,
repositories}.py`, `adapters/db/migrations/`, `config/settings.py`.

**Interfaces.** All Protocols in `ports/`. Add `errors.py`:
`TaskFlowError`, `ProviderError`, `SchemaError`, `AllProvidersFailed`,
`RetrievalUnavailable`, `ConflictError`, `TransientError`.

**Steps.**
1. Review `domain/models.py` and `ports/` against `04_SCHEMAS.md`; fix any drift.
2. `config/settings.py` with `pydantic-settings`, loading `.env` + `config/*.yaml`.
3. `adapters/db/engine.py` with the WAL pragma listener (`journal_mode=WAL`,
   `busy_timeout=5000`, `foreign_keys=ON`).
4. `adapters/db/orm.py`: `inbox`, `traces`, `trace_events`, `reviews`, `edit_records`,
   `outbox`, `audit`, `llm_calls`, `alerts`. Every table has `tenant_id`.
   `inbox.dedupe_key` and `outbox.idempotency_key` are UNIQUE.
5. `alembic init`; generate revision `0001_initial`.
6. Repository implementations for each Protocol.

**Tests.** Model validation (including `DraftOutput` rejecting an uncited citation);
deterministic `doc_id`; repository round-trips against a temp SQLite file;
`try_claim` returns False on a duplicate key.

**Verification.** `make migrate && make test && make typecheck`, then
`sqlite3 data/taskflow.db ".tables"`.

**Definition of done.**
- [ ] Migration creates all nine tables with `tenant_id` everywhere
- [ ] Both UNIQUE constraints exist and are covered by a test
- [ ] Every Protocol in `ports/` has exactly one implementation in `adapters/db/`
- [ ] `04_SCHEMAS.md` matches the code

**Failure modes.** Schema churn later is expensive — freeze `DraftOutput` and `Trace` now.
SQLite `database is locked` → the WAL pragma listener isn't attached to the right engine.

---

## P2 — Walking skeleton · ~2 days · 🚶 **the phase that de-risks everything**

**Goal.** One message goes in through the console and comes out with a persisted,
inspectable trace. Stubs are fine — the *path* must be real. **You must be able to
screen-share something on day 4.**

**Prerequisites.** P1.
**Reading.** `01_ARCHITECTURE.md` §The pipeline, §The decision in two layers.

**Files.** `adapters/channels/console.py`, `adapters/llm/{base,claude}.py`,
`services/classify/service.py` (keyword stub), `services/retrieve/service.py`
(in-memory over 5 hardcoded chunks), `services/draft/service.py`,
`services/validate/{pii_leak,citations,runner}.py`, `services/confidence/{gates,scorer}.py`,
`services/route/service.py`, `services/tracing/service.py`,
`pipeline/{state,orchestrator}.py`, `scripts/run_scenario.py`.

**Interfaces.**
```python
@dataclass(frozen=True)
class PipelineState:
    message: InboundMessage
    trace_id: str
    intent: Intent | None = None
    intent_confidence: float = 0.0
    retrieval: RetrievalResult | None = None
    draft: DraftOutput | None = None
    validators: tuple[ValidatorResult, ...] = ()
    confidence: ConfidenceBreakdown | None = None
    decision: RoutingDecision | None = None

async def run_pipeline(msg: InboundMessage, deps: Deps) -> RoutingDecision: ...
```

**Steps.**
1. `PipelineState` as a frozen dataclass; stages return a new state via `replace()`.
2. `orchestrator.py`: explicit `async def run_pipeline` with the branch points from
   `01_ARCHITECTURE.md`. Target ~60 lines. No business logic — one call per stage.
3. Minimal Claude adapter using `messages.parse(output_format=DraftOutput)`.
4. Deterministic validators only: PII regex, citation resolution.
5. **`gates.py` and `scorer.py` in full** — this is the real thing, not a stub. Write
   `tests/unit/test_gates.py` **first**.
6. Trace written at every stage, with elapsed ms.
7. `run_scenario.py --text "..."` prints intent, chunk count, gate results, score,
   decision, trace id, cost, elapsed.

**Tests.** `tests/pipeline/` with `FakeLLMProvider` returning canned JSON:
auto-send on a clean draft; human review on a PII leak; human review on an
unresolvable citation. Plus the five canonical gate tests in `tests/unit/test_gates.py`.

**Verification.**
```bash
make scenario S=billing_double
sqlite3 data/taskflow.db "select stage, elapsed_ms from trace_events order by id"
```

**Definition of done.**
- [ ] End-to-end run persists a trace with every stage present
- [ ] `test_critical_policy_violation_never_auto_sends` passes
- [ ] The orchestrator is under 120 lines and readable top to bottom
- [ ] You can demo this to someone on day 4

**Failure modes.** Scope creep into P5/P7 — resist; stubs are correct here. A frozen
dataclass with `replace()` avoids the whole class of "who mutated the state" bugs.

---

## P3 — Knowledge base, datasets, ingestion · ~2 days

**Goal.** Six datasets, versioned and leakage-checked; 42 documents searchable in
embedded Qdrant.

**Prerequisites.** P0 (can overlap P2).
**Reading.** `06_DATA_GENERATION.md`, `07_EVAL_METHODOLOGY.md` §Leakage.

**Files.** `scripts/{generate_kb,generate_datasets,ingest_kb,check_leakage}.py`,
`services/retrieve/chunking.py`, `adapters/vector/qdrant_store.py`,
`data/knowledge_base/*.md`, `data/datasets/*.jsonl`, `data/manifests/*.json`.

**Interfaces.** `VectorStore` implemented against `QdrantClient(path=settings.qdrant_path)`.

**Steps.**
1. Generate 42 KB documents (use the `kb-writer` subagent; prompt in
   `config/prompts/datagen_kb.md`). **Leave the intentionally-absent topics out.**
2. `chunking.py`: heading-aware split, ~450 tokens, 60 overlap. Deterministic ids:
   `doc_id = sha256(relpath + "\n" + title)[:16]`, `chunk_id = f"{doc_id}:{ordinal}"`.
3. Create the collection: named vectors `dense` (384d, cosine) and `bm25`
   (`SparseVectorParams(modifier=Modifier.IDF)`); payload indexes on `tenant_id`,
   `doc_type`, `product_tier`, `intents`, `doc_id`.
4. Ingest with `models.Document(text=..., model=...)` so fastembed computes both vectors
   locally. Point ids are `uuid5(chunk_id)` so re-ingest is idempotent.
5. Generate `classifier_train` + `classifier_validation` with **Ollama** (generator A).
6. Generate `classifier_test` with **Claude** (generator B), different prompt, different
   personas. This is the methodological point — see `07_EVAL_METHODOLOGY.md`.
7. Hand-write `golden_eval.jsonl` (60–80 rows) with `expected_intent` and
   `expected_doc_ids`. **You write these, not a model.**
8. `demo_scenarios.jsonl` with the exact five texts.
9. Manifests: name, version, rows, sha256, generator model, prompt hash, date.
10. `check_leakage.py`: exact-duplicate and cosine>0.95 near-duplicate check across every
    pair of splits; exits non-zero on a hit.

**Tests.** Chunker boundary cases; `doc_id` stability; frontmatter validation;
integration: ingest → `hybrid_search("reset password")` returns the password doc in top 3;
re-ingest does not change the point count.

**Verification.** `make kb && make ingest && make datasets` — leakage check exits 0.

**Definition of done.**
- [ ] 42 documents ingested; point count recorded in `docs/metrics/`
- [ ] Six datasets exist with manifests
- [ ] Leakage check green, **and you have verified it fails when you paste a train row
      into the test file** (an oracle that never fires is not an oracle)
- [ ] At least three topics deliberately absent from the KB

**Failure modes.** Synthetic KB too uniform → force structural variety in the prompt.
Chunk boundaries separating a limit from its exception → heading-first split plus overlap.

---

## P4 — Providers, router, offline mode · ~1 day

**Goal.** Two providers behind one Protocol, cost tracked, and a demo that runs offline.

**Prerequisites.** P2, P3.
**Reading.** `01_ARCHITECTURE.md` §Providers, ADR-002.

**Files.** `adapters/llm/{base,claude,ollama,router}.py`, `services/cost/service.py`,
`config/providers.yaml`.

**Interfaces.**
```python
class ProviderRouter:
    async def complete_structured[T: BaseModel](
        self, *, purpose: str, system: str, user: str, schema: type[T]
    ) -> tuple[T, LLMCall]: ...
```

**Steps.**
1. Claude adapter via `messages.parse(output_format=schema)`. Raise `SchemaError` when
   `stop_reason` is `refusal` or `max_tokens` — the docs are explicit that output may not
   match the schema in those two cases.
2. Ollama adapter with `format=schema.model_json_schema()`.
3. Router: walk `priority[mode]`, catch `(TransportError, TimeoutError, SchemaError)`,
   fall through, raise `AllProvidersFailed` at the end. Record `provider_used`,
   `failed_over`, `latency_ms`, `cost_usd` on every call.
4. Cost service: per-message, per-day totals; daily cap trips template mode + alert.
5. Measure and record local generation latency for a 300-word draft.

**Tests.** Router failover with a provider that always raises; `local_only` mode never
touches Claude; `SchemaError` on a simulated refusal; no hardcoded model names.
Live-marked: one real call per provider.

**Verification.**
```bash
ANTHROPIC_API_KEY=broken make scenario S=billing_double   # served by ollama
TASKFLOW_LLM_MODE=local_only make scenario S=billing_double
```

**Definition of done.**
- [ ] Killing the Claude key still produces a valid draft
- [ ] `local_only` completes all reachable scenarios with the network off
- [ ] Local latency measured and recorded; model size decision made
- [ ] Cost appears on every trace

**Failure modes.** Ollama not running → clear error naming `ollama serve`, not a stack
trace. Local model too slow → drop to 3B now, not the week of the demo.

---

## P5 — Real retrieval · ~1.5 days

**Goal.** Replace the P2 stub, and measure the result.

**Prerequisites.** P3, P4.
**Reading.** `07_EVAL_METHODOLOGY.md` §Retrieval.

**Files.** `adapters/vector/qdrant_store.py` (hybrid query), `services/retrieve/
{service,query_builder,sufficiency,injection}.py`, `scripts/eval_retrieval.py`.

**Steps.**
1. One `query_points` call: two `prefetch` branches (dense k=20, bm25 k=20) +
   `FusionQuery(fusion=Fusion.RRF)` + a `tenant_id` filter, limit 20.
2. Cross-encoder rerank (`ms-marco-MiniLM-L-6-v2`) → top 5. Load the model at startup.
3. Deterministic query builder: thread context + alias expansion from `settings.yaml`.
   **No LLM here.**
4. Sufficiency: `top_rerank >= min_top_score AND support_count >= min_support_count`.
   Insufficient → log a `retrieval_gap` row → human review. **One pass, no loop.**
5. `injection.py`: scan retrieved chunks for instruction-like patterns → sets
   `suspicious_context` → gate G7.
6. `eval_retrieval.py`: recall@5, MRR@10, gap-rate, and hybrid vs dense-only.

**Tests.** RRF and rerank ordering with fake scores; sufficiency at threshold boundaries;
alias expansion; injection detection positive and negative; integration: hybrid beats
dense-only on an exact-term query such as an error code.

**Verification.** `make eval`; `make scenario S=sso_question` → retrieval gap → human.

**Definition of done.**
- [ ] recall@5, MRR@10, gap-rate published in `docs/metrics/retrieval.md`
- [ ] Hybrid-vs-dense delta recorded (this justifies the sparse branch with a number)
- [ ] A query with no supporting document escalates instead of drafting

**Failure modes.** Thresholds tuned to the demo scenarios → tune on `golden_eval`, then
check the scenarios still pass. Reranker cold start → warm at startup, assert in preflight.

---

## P6 — Intent classifier · ~1.5 days

**Goal.** A trained classifier with defensible metrics and an abstain path.

**Prerequisites.** P3.
**Reading.** `07_EVAL_METHODOLOGY.md` §Classifier.

**Files.** `ml/{embeddings,train_classifier,evaluate,calibrate}.py`, `ml/artifacts/`,
`services/classify/service.py`.

**Steps.**
1. Embed with frozen MiniLM; **cache embeddings to `.npy`** or you will re-embed on every
   run and waste minutes each time.
2. `LogisticRegression(class_weight="balanced")`, grid over C on the validation split.
3. Choose the abstain threshold τ from the validation precision/coverage curve
   (target precision ≈ 0.95); default 0.55 in `thresholds.yaml`.
4. Evaluate on **both** `classifier_test` and `golden_eval`: accuracy, macro-F1, per-class
   precision and recall, confusion matrix PNG, abstain rate.
5. Persist the artifact with a version and the training manifest sha.
6. Wire into the pipeline: `intent_confidence` feeds the score, abstention feeds G5.
7. Write `ml/artifacts/expected_metrics.json` as the regression guard.

**Tests.** Abstain boundary; artifact version recorded on the trace; all five demo
scenarios classify correctly; regression test against `expected_metrics.json` with a
0.03 tolerance and a hard floor of 0.85 on complaint recall.

**Verification.** `make train && cat docs/metrics/classifier.md`.

**Definition of done.**
- [ ] Both metric sets published **with the gap stated explicitly**
- [ ] Confusion matrix committed
- [ ] Abstain path exercised by a test and by `make scenario S=gibberish`
- [ ] Complaint recall ≥ 0.85 (this is the safety-critical class)

**Failure modes.** Near-perfect synthetic accuracy is expected, not a triumph — the golden
number is the honest one. Complaint/billing confusion is the costly error; check it
specifically.

---

## P7 — Validators, policy engine, gates · ~2 days

**Goal.** The governance layer, complete and tested.

**Prerequisites.** P4, P5.
**Reading.** `05_POLICY_RULES.md` (build the engine *from* it), ADR-001, ADR-003.

**Files.** `services/validate/{policy,grounding,pii_leak,citations,tone,completeness,
runner}.py`, `services/confidence/{gates,scorer}.py`, `config/policies.yaml`.

**Interfaces.**
```python
class Validator(Protocol):
    name: str
    blocking: bool
    async def validate(self, ctx: ValidationContext) -> ValidatorResult: ...
```

**Steps.**
1. Policy engine with detector types `amount_over`, `regex_any`, `regex_absent`,
   `price_mismatch`. Critical severity ⇒ G1.
2. PII regex suite; **add a Luhn check on card-like matches** or order numbers will
   false-positive and block everything.
3. Citation coverage (score) and validity (G3).
4. Grounding validator — the single LLM call. Per-sentence entailment; `contradicted`
   or ratio below `min_grounding_ratio` blocks.
5. Tone and completeness as deterministic heuristics per `config/prompts/quality_rubric.md`.
6. `runner.py`: `asyncio.gather` with a per-validator timeout; any exception or timeout
   yields `ValidatorResult(passed=False, errored=True, reason="validator_error")` ⇒ G6.
7. Wire gates → scorer → decision; persist the full `ConfidenceBreakdown`.

**Tests.** Two per policy rule (20 total). The five canonical gate tests. Validator
timeout ⇒ human review. Parallel runner returns all five results even when one raises.

**Verification.** `make scenario S=refund_750` → `HUMAN_REVIEW`,
`reason_code=G1_policy_critical`, and **score above 0.85** in the same output.

**Definition of done.**
- [ ] Every policy rule has a passing and a failing test
- [ ] Changing a threshold in `thresholds.yaml` changes routing with no code edit
- [ ] Auto-send rate on `golden_eval` measured and between 40% and 60%

**Failure modes.** Over-blocking → tune *thresholds*, never gates. Grounding disagreeing
with citation coverage is by design; log both.

---

## P8 — Human review, delivery, SLA · ~2 days

**Goal.** The human loop is real: full context, three actions, exactly-once delivery,
escalation on breach.

**Prerequisites.** P7.
**Reading.** `08_OBSERVABILITY.md` §Review panes.

**Files.** `api/{main,deps,auth}.py`, `api/routers/{reviews,messages,health}.py`,
`services/review/service.py`, `services/delivery/{outbox,service}.py`,
`services/sla/scheduler.py`, `adapters/alerts/{teams,fallback}.py`,
`dashboard/app.py`, `dashboard/pages/1_review_queue.py`.

**Interfaces.**
```
POST /api/reviews/{id}/approve  {reviewer}
POST /api/reviews/{id}/edit     {reviewer, edited_text, reason?}
POST /api/reviews/{id}/reject   {reviewer, reason}
GET  /api/reviews?state=pending&sort=sla_deadline
```

**Steps.**
1. FastAPI app with `lifespan` starting the four background loops and warming models.
2. Bearer-token auth — **even locally**. Streamlit authenticates like any other client.
3. Review service with optimistic locking:
   `UPDATE reviews SET state=:to WHERE id=:id AND state=:expected`, `rowcount == 1` wins.
4. `EditRecord` stores both texts on every edit — this is the deferred fine-tuning data.
5. Outbox: `enqueue` returns False on a duplicate `idempotency_key`; `claim` moves
   QUEUED→SENDING in one UPDATE; Gmail send sets `In-Reply-To`/`References`.
6. SLA loop: 30s tick, `due()` → transition to ESCALATED → alert.
7. Teams alerter (Adaptive Card via Workflows webhook) + fallback alerter, behind
   `AlertChannel`.
8. Streamlit review page with all nine panes (see `08_OBSERVABILITY.md`).

**Tests.** SLA computation including the breach edge; outbox never sends twice under a
simulated retry; approve→sent transition; edit persists both texts; double-approve yields
one send and one conflict.

**Verification.** `make api & make dash &`, run `refund_750`, approve it in Streamlit,
then `sqlite3 data/taskflow.db "select state, count(*) from outbox group by 1"` → one sent.

**Definition of done.**
- [ ] All nine review panes render from real data
- [ ] SLA breach posts an alert (Teams or fallback)
- [ ] Forced double-approve produces exactly one delivery
- [ ] Streamlit performs zero direct database writes

**Failure modes.** Streamlit reruns double-submitting → idempotency key per action and
disable the button on submit. Timezones → store UTC, render local.

---

## P9 — Real ingestion: Gmail, web chat, queue · ~2 days

**Goal.** Real messages from two channels, exactly once, with retries you can show.

**Prerequisites.** P1, P8. Credentials from P-1.
**Reading.** `09_FAILURE_MODES.md`.

**Files.** `adapters/bus/sqlite_queue.py`, `adapters/channels/{gmail,webchat}.py`,
`api/routers/chat.py`, `api/templates/chat.html`, `worker/{loop,pollers}.py`,
`services/preprocess/{pii,spam,thread,fastpath}.py`.

**Steps.**
1. SQLite queue adapter: `enqueue` (UNIQUE ⇒ dedupe), `claim_next`
   (`UPDATE ... RETURNING` with a lease), `ack`, `dead_letter`, `pending_count`.
2. Worker loop consuming the queue and calling `run_pipeline`.
3. Gmail poller with a persisted `history_id` cursor, HTML→text, **quoted-reply
   stripping** (without it, turn 3 of a thread re-embeds the whole history and retrieval
   collapses).
4. Web chat: one HTML page served at `/chat`, `POST /api/chat/messages`, polling or SSE
   for the reply, `conversation_id` in a cookie.
5. Preprocess chain: PII redaction → spam rules → thread load → fast path
   (normalized-question hash against `config/prompts/templates.yaml`).
6. Retry policy: 3 attempts, then dead-letter + alert.

**Tests.** Normalization per channel from recorded fixtures; dedupe under concurrency;
quoted-reply stripping; fast-path normalization; integration: enqueue → claim → ack;
lease reclaim after a simulated crash.

**Verification.** Email yourself → trace within 30 s. Same email twice → one trace and one
`duplicate_dropped` event. Kill the API mid-message, restart → reprocessed, one reply.

**Definition of done.**
- [ ] Both channels ingest end-to-end
- [ ] Duplicates suppressed at ingest *and* at delivery
- [ ] Crash-and-restart reprocesses without double-sending
- [ ] Fast path answers scenario 1 with zero LLM calls

**Failure modes.** Gmail token expiry — check it in preflight. Poller overlap → single
task with a lock.

---

## P10 — Observability, resilience, hardening · ~3 days

**Goal.** Answer "why did it send this?" and "why was this escalated?" in the UI, survive
a killed dependency, and rehearse.

**Prerequisites.** P8, P9.
**Reading.** `08_OBSERVABILITY.md`, `09_FAILURE_MODES.md`, `12_DEMO_SCRIPT.md`.

**Files.** `dashboard/pages/{2_traces,3_metrics,4_gaps,5_health}.py`,
`adapters/resilience/circuit_breaker.py`, `scripts/{preflight,seed_demo,record_demo}.py`,
`tests/e2e/test_scenarios.py`, `docs/12_DEMO_SCRIPT.md`, `docs/13_*.md`.

**Steps.**
1. Trace viewer: one page per `trace_id`, five expanders, decision stated at the top.
2. Cost tile, retrieval gap analyzer, health page (breakers, queue depth, heartbeats).
3. Generic `@circuit_breaker` on the three external adapters, with the degraded-mode
   matrix from `09_FAILURE_MODES.md`.
4. E2E tests for the five scenarios asserting **reason codes and LLM-call counts**,
   never prose.
5. `preflight.py` with the full check list from `12_DEMO_SCRIPT.md`.
6. Seed the demo database and commit the snapshot.
7. Record a backup video of a full clean run.
8. Two full rehearsals from cold, on different days.

**Tests.** Breaker state transitions; trace-completeness schema test; with Ollama stopped
and the Claude key broken, a message still routes to human with a clear reason.

**Verification.** `make preflight` exits 0; `make test-e2e` → 5 passed; the
`/demo-rehearsal` workflow completes twice.

**Definition of done.**
- [ ] Both trace questions answerable in under 30 seconds on any message
- [ ] Offline drill passes: `TASKFLOW_LLM_MODE=local_only`, network off, 5/5 scenarios
- [ ] Provider-failure drill degrades and alerts
- [ ] Backup recording exists; `make demo-reset` rehearsed
- [ ] All 14 docs current

**Failure modes.** Trace bloat → store prompt hashes and truncated bodies, full text
behind a toggle. Leaving rehearsal to the last afternoon — schedule it on day 18.

---

## Dependency order

```
P0 → P1 → P2 ┬→ P4 → P5 ┐
P0 → P3 ──────┴→ P6 ─────┴→ P7 → P8 → P9 → P10
```

Critical path: `P0 → P1 → P2 → P4 → P5 → P7 → P8 → P9 → P10`.
P3 dataset generation and P6 training both run unattended — start them and keep coding.

## Calendar

| Week | Days | Phases |
|---|---|---|
| 1 | 1–5 | P0, P1, **P2 walking skeleton by day 4**, P3 starts |
| 2 | 6–10 | P3 finishes, P4, P5, P6 |
| 3 | 11–15 | P7, P8, P9 starts |
| 4 | 16–20 | P9 finishes, P10, **two rehearsals, buffer** |

## Scope cuts, in order

Cut in this order if behind. Do not improvise a different order under pressure.

1. Classifier calibration curve (0.5 d)
2. Retraining-from-edits script (1 d) — show the `EditRecord` rows instead
3. Retrieval gap analyzer page (0.5 d) — query it in SQL live
4. Metrics and health dashboard pages (1 d) — keep the trace viewer
5. Cross-encoder reranker (0.5 d) — report the recall drop honestly
6. Web chat channel (1 d) — demo email-only; the connector interface still proves the point
7. Circuit-breaker live drill (0.5 d) — describe the degraded matrix instead

**Never cut:** the walking skeleton, gates-before-score, the trace viewer, human review
with real approve/edit/reject, the held-out classifier evaluation, the five scenarios,
`make preflight`, the offline mode.
