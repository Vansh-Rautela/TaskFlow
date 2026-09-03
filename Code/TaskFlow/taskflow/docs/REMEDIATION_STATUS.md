# TaskFlow Remediation Status

Takeover audit and remediation record. Statuses are evidence-backed: an item is
`Fixed` only when a named test or command demonstrates the corrected behaviour.

Authority for intended behaviour: `docs/BRD.md` (§7 functional requirements,
§14 risk register, §15 known-defects register). Where code and documentation
disagreed, the BRD won and the code was changed.

## 1. Functionality Matrix (as found, before remediation)

| # | Feature | Intended behaviour | Actual behaviour as found | Status | Evidence |
|---|---|---|---|---|---|
| F-01 | Inbound ingestion (REST) | `POST /messages` runs the pipeline | Works | WORKING | `tests/unit/test_api.py` |
| F-02 | Inbound ingestion (Gmail IMAP) | Poll unseen mail, run pipeline | Works; no dedup, no smoke test | PARTIALLY_IMPLEMENTED | `adapters/channels/gmail_poller.py` |
| F-03 | PII redaction before LLM | Redacted text reaches every LLM call | Classification gets redacted text; **drafting gets raw `body_text`** | BROKEN | `services/draft/service.py:45` (BRD R-1) |
| F-04 | Intent classification (LLM) | 8-intent taxonomy from the `Intent` enum | Prompt enumerates 10 intents; `cancellation`/`enterprise` fail validation → silent keyword fallback | BROKEN | `services/classify/service.py:16-18` (BRD R-2) |
| F-05 | Intent classification (fallback) | Deterministic keyword classifier | `cancel`/`close account` routes to `REFUND` | BROKEN | `services/classify/service.py:57-58` |
| F-06 | Classification failover | Any provider can classify | Only OpenRouter defines a `classification` model; `KeyError` escapes the router's `except` | BROKEN | `adapters/llm/router.py:55` (BRD R-8) |
| F-07 | Hybrid retrieval | Dense + sparse RRF over Qdrant | Works | WORKING | `tests/integration/test_pipeline_e2e.py` |
| F-08 | Retrieval sufficiency | Config-driven top-score / support-count gate | Works | WORKING | `tests/unit/test_retrieval.py` |
| F-09 | Prompt-injection detection | Suspicious chunk text vetoes via G7 | Works | WORKING | `tests/unit/test_retrieval.py` |
| F-10 | Drafting | Grounded draft with resolvable citations | Works; template fallback on provider exhaustion | WORKING | `tests/unit/test_drafting.py` |
| F-11 | Validators | PII / citations / grounding, fail-closed | Works | WORKING | `services/validate/runner.py` |
| F-12 | Policy engine | 10 rules over inbound and outbound text | Works; `price_mismatch` matches plan names as substrings | PARTIALLY_IMPLEMENTED | `services/policy/service.py:100` |
| F-13 | Safety gates (G1–G7) | Conjunctive vetoes before scoring | Works | WORKING | `tests/unit/test_gates.py` |
| F-14 | Quality score | 5 weighted signals | `tone_alignment` is hardcoded to `1.0` — a nonexistent measurement contributing 0.10 to every routing decision | MISLEADING | `pipeline/orchestrator.py:136` |
| F-15 | Per-intent thresholds | Threshold selected by intent | `thresholds["general"]` applied universally | NOT_IMPLEMENTED | `pipeline/orchestrator.py:143` (BRD R-3) |
| F-16 | Human review queue | Persist, list, optimistic-lock transitions | Works | WORKING | `tests/unit/test_review_worker.py` |
| F-17 | Review SLA deadline | Per-intent from `sla_minutes` config | Hardcoded `timedelta(hours=4)` | NOT_IMPLEMENTED | `pipeline/orchestrator.py:185` (BRD R-4) |
| F-18 | SLA breach sweeper | Periodic `due()` sweep + alert | `due()` implemented, never called | NOT_IMPLEMENTED | BRD NFR-5.3 |
| F-19 | Outbox enqueue | UNIQUE idempotency key | Constraint exists; `IntegrityError` handler commits a poisoned session | BROKEN | `adapters/db/repositories.py:418` |
| F-20 | Outbox delivery | Mark SENT only on real delivery | `deliver_message()` falls through to `return True`; a failed or skipped send is recorded `SENT` | FAKE_SUCCESS | `worker/outbox_worker.py:41` (BRD R-5) |
| F-21 | Outbox retry / lease / DEAD | Retry to `max_attempts`, then DEAD | No retry, no lease, no requeue; `FAILED` is terminal; crashed worker strands rows in `SENDING` | NOT_IMPLEMENTED | BRD §5.3 |
| F-22 | Inbound deduplication | Enforced before pipeline execution | Two competing repository implementations, neither called by the pipeline | NOT_IMPLEMENTED | BRD FR-1.4 |
| F-23 | Alerting | Escalations reach a human | Works — but ships the **unredacted** customer body and a full `model_dump()` to Telegram, Slack and a generic webhook | BROKEN (security) | `services/alert/service.py:118,194` |
| F-24 | Alert failure handling | Failures are visible | `except Exception: pass` in the orchestrator | MISLEADING | `pipeline/orchestrator.py:194` |
| F-25 | Trace reconstructability | Trace explains what happened | Stage events persist `payload={}` — timing only | MISLEADING | `pipeline/orchestrator.py:52` |
| F-26 | Cost ledger | Per-call USD cost | Works; Claude priced `0.0/0.0`, and prices are duplicated in adapter `capabilities` | PARTIALLY_IMPLEMENTED | `config/providers.yaml` (BRD R-10) |
| F-27 | Budget enforcement | `max_llm_calls_per_message`, `daily_spend_usd` | `budgets` block read by no code | NOT_IMPLEMENTED | BRD NFR-5.2 |
| F-28 | Schema / migrations | Alembic is the single source of truth | API and dashboard call `Base.metadata.create_all`, bypassing Alembic | BROKEN | `api/app.py:47` |
| F-29 | API authentication | `TASKFLOW_API_TOKEN` guards the API | Setting exists; no endpoint checks it | NOT_IMPLEMENTED | `config/settings.py:23` |
| F-30 | Telegram channel connector | Two-way Telegram | Module cannot import: `Channel.TELEGRAM` and `DeliveryReceipt` do not exist | DEAD_CODE | `adapters/channels/telegram.py` (BRD R-7) |
| F-31 | `src/taskflow/ml/` | Trained classifier | Empty package; `scikit-learn`/`joblib`/`numpy` declared and unused | DEAD_CODE | BRD §5.3 |
| F-32 | `ports/cache.py`, `ports/event_bus.py` | Cache / bus abstractions | Zero implementations, zero references | DEAD_CODE | grep: 0 usages |
| F-33 | `adapters/{bus,cache,resilience}/` | — | Empty packages | DEAD_CODE | grep: 0 usages |
| F-34 | Architecture boundary | `pipeline` depends on ports, not adapters | Orchestrator imports `QdrantVectorStore` and `ProviderRouter`; `ports/` imports from `services/` | BROKEN | `pipeline/orchestrator.py:13-14` |
| F-35 | Makefile | Documented commands work | Targets reference `taskflow.api.main`, `scripts/generate_kb.py`, `scripts/preflight.py`, `taskflow.ml.*`, `data/snapshots/demo_seed.db` — none exist | BROKEN | `Makefile` |
| F-36 | `scripts/verify_system.py` | Verify the real system | Fakes LLM responses inline and references `Intent.DISPUTE`, which does not exist → crashes offline | FAKE_SUCCESS | `scripts/verify_system.py:84` |
| F-37 | `tests/e2e/` | Five demo scenarios | Empty | NOT_IMPLEMENTED | BRD R-14 |
| F-38 | Committed credentials | No secret in the repo | Tracked `.env.example` contains a live-looking Gmail address and app password | BROKEN (security) | `.env.example` (BRD R-9) |
| F-39 | `tests/fixtures/fakes.py::FakeVectorStore` | Test double for `VectorStore` | Implements `hybrid_search`/`count`; the port defines `search`/`ingest`/`setup_collection`. Unusable | DEAD_CODE | port mismatch |
| F-40 | Spam filter / FAQ fast path | `REJECTED_SPAM`, `TEMPLATE_SENT` | Config blocks and enum members exist; no implementation | NOT_IMPLEMENTED | BRD §5.3 (deliberate deferral) |

## 2. Remediation Ledger

Every `Fixed` row names a test or command that demonstrates the corrected behaviour.

### Security and data safety

| ID | Problem | Severity | File | Status | Verification |
|---|---|---|---|---|---|
| SEC-001 | A live-looking Gmail address and app password committed in the git-tracked `.env.example` | Critical | `.env.example` | **Scrubbed — rotation outstanding, see §4** | `git ls-files` + file contents; the credential is still live in history at `52dae4b` |
| SEC-002 | The drafting prompt interpolated `msg.body_text`, so raw card numbers and SSNs were sent to the provider on every drafted message | Critical | `services/draft/service.py` | Fixed — uses `body_redacted` | `test_drafting_prompt_never_receives_unredacted_pii` |
| SEC-003 | Alert dispatch shipped the unredacted customer body to Telegram, Slack and email, and posted a full `item.model_dump()` — draft, body, confidence breakdown — to an arbitrary webhook URL | Critical | `services/alert/service.py` | Fixed — body re-redacted at the boundary; webhook payload reduced to identifiers, reason code and deadline | `test_pii_is_redacted_everywhere_it_could_escape` |
| SEC-004 | Running the test suite against a populated `.env` dispatched **real** alert emails through the developer's live Gmail account | Critical | `tests/` | Fixed — session-wide autouse fixture blanks every credential | `tests/conftest.py::hermetic_environment`; observed live during the audit |
| SEC-005 | `TASKFLOW_API_TOKEN` was a configured setting no endpoint enforced; the entire API was unauthenticated | Critical | `api/app.py` | Fixed — bearer auth on every endpoint except `/` and `/healthz` | `test_protected_endpoints_reject_anonymous_requests` (6 endpoints), `test_wrong_token_is_rejected` |
| SEC-006 | The review row persisted the raw inbound body, which the alerter then forwarded onward | High | `pipeline/orchestrator.py` | Fixed — stores `body_redacted` | `test_review_item_stores_redacted_body` |

### Correctness — routing and safety

| ID | Problem | Severity | File | Status | Verification |
|---|---|---|---|---|---|
| BUG-001 | `deliver_message()` fell through to `return True`, so a failed or skipped SMTP send was recorded `SENT` and the customer's reply was silently lost | Critical | `worker/outbox_worker.py` | Fixed — rewritten around a channel that returns a receipt or raises | `test_transport_failure_is_retried_then_dead_never_sent`, plus a live SMTP handshake with rejected credentials |
| BUG-002 | The template fallback fabricated a citation to a chunk it never used, scoring 1.00 coverage; a content-free stub reached 0.862 against a 0.70 bar and was stopped only by an unrelated policy veto | Critical | `services/draft/service.py` | Fixed — no citation, and new Gate **G8** blocks any draft no model produced | `test_template_fallback_is_never_auto_sent`, `test_template_fallback_would_otherwise_clear_the_bar` |
| BUG-003 | `tone_alignment` was hardcoded to `1.0`; a tenth of every routing decision rested on a measurement nobody was taking | High | `pipeline/orchestrator.py` | Fixed — implemented as `services/confidence/tone.py`, deriving from warning-severity policy violations and intent-appropriate tone | `tests/unit/test_tone.py` (8 tests, incl. `test_the_signal_is_not_a_constant`) |
| BUG-004 | Per-intent thresholds were configured but `thresholds["general"]` was applied to everything, so `refund` auto-sent at 0.70 instead of 0.90 | High | `pipeline/orchestrator.py` | Fixed — `settings.threshold_for(intent)` | `test_threshold_is_selected_per_intent` |
| BUG-005 | The keyword classifier routed `cancel` / `close account` / `erase my data` to `REFUND` | High | `services/classify/service.py` | Fixed — routes to `ACCOUNT`; there is no `cancellation` intent in the 8-intent taxonomy | `test_cancellation_routes_to_account_not_refund` |
| BUG-006 | The classifier prompt listed 10 intents, two of which (`cancellation`, `enterprise`) are not `Intent` members; those labels failed validation and silently degraded to the keyword path | High | `services/classify/service.py` | Fixed — prompt generated from the enum | `test_prompt_enumerates_exactly_the_intent_enum` |
| BUG-007 | Keyword-fallback confidences reached 0.98, clearing the G5 abstain gate on the strength of a substring match | High | `config/thresholds.yaml` | Fixed — capped at 0.80 | `test_fallback_confidence_never_exceeds_a_confident_llm` |
| BUG-008 | `model_for` raised `KeyError` for a provider with no model for a purpose; the router did not catch it, so classification could never fail over | High | `adapters/llm/router.py`, `config/settings.py`, `config/providers.yaml` | Fixed — raises `ConfigError`, the router skips that provider, and every provider now declares a `classification` model | `test_a_provider_missing_a_model_is_skipped_not_fatal`; verified live (OpenRouter 401 → Claude unset → Ollama answered) |
| BUG-009 | An adapter leaking an untranslated exception aborted the whole provider chain | Medium | `adapters/llm/router.py` | Fixed — logged at error level, failover continues | `test_an_untranslated_adapter_error_still_fails_over` |
| BUG-010 | Deduplication existed at the repository layer but nothing called it; the same inbound message was answered twice | High | `pipeline/orchestrator.py` | Fixed — `try_claim` before any other work, returns `DROPPED_DUPLICATE` | `test_duplicate_inbound_produces_one_reply`, `test_duplicate_ingest_is_dropped` |
| BUG-011 | Review SLA was hardcoded to 4 hours; `sla_minutes` was configured and ignored, and `due()` was never called | Medium | `pipeline/orchestrator.py` | Fixed — `sla_minutes_for(intent)` plus `services/sla` sweeper | `test_sla_breach_escalates_an_overdue_review`, `test_human_review_approve_then_deliver` |
| BUG-012 | The citations validator returned `passed=True` with a sub-threshold score when a draft cited nothing, reading as a failure in the trace while routing as a success | Medium | `services/validate/citations.py` | Fixed — vetoes when context was available and uncited; partial coverage compared against the configured minimum | `tests/unit/test_drafting.py` |
| BUG-013 | Approving a review with no `sender_email` enqueued a reply to `support-fallback@taskflow.dev`, which the outbox would mark `SENT` | Medium | `services/review/service.py` | Fixed — no address, no outbound row, error logged | `test_approve_without_sender_email_enqueues_nothing` |
| BUG-014 | `except Exception: pass` swallowed alert-dispatch failures | Medium | `pipeline/orchestrator.py` | Fixed — recorded on the trace, logged, and a no-channel-succeeded case is distinguished | code review; `_create_review` |
| BUG-015 | `price_mismatch` tested `plan in draft_text_lower`, so "pro" matched inside "provide" and flagged unrelated dollar figures anywhere in the draft | Low | `services/policy/service.py` | Fixed — word-boundary match within an 80-character proximity window | `tests/unit/test_policy.py` |

### Persistence and state

| ID | Problem | Severity | File | Status | Verification |
|---|---|---|---|---|---|
| BUG-016 | `IntegrityError` was caught inside an open transaction whose context manager then committed, so an ordinary duplicate raised on the way out | High | `adapters/db/repositories.py` | Fixed — explicit `rollback()` before returning `False` | `test_idempotency_key_blocks_a_second_identical_enqueue` |
| BUG-017 | Two competing dedup implementations (`SQLiteProcessedMessageRepository` and `SQLiteInboxRepository.try_claim`) whose reservations collided with each other | High | `adapters/db/repositories.py` | Fixed — one implementation; `save()` fills the reserved row | `test_duplicate_inbound_produces_one_reply` |
| BUG-018 | `create_all()` at API and dashboard startup bypassed Alembic; the schema depended on which mechanism ran first | High | `api/app.py`, `dashboard/app.py` | Fixed — `adapters/db/bootstrap.py` runs Alembic; `create_all` survives only in tests | `tests/integration/test_migrations.py` (5 tests) |
| BUG-019 | Alembic's `env.py` ignored the URL a programmatic caller set on the Config and read `DATABASE_URL` instead, so `run_migrations(url)` migrated the wrong database | High | `adapters/db/migrations/env.py` | Fixed — caller-supplied URL takes precedence | `test_migrations_and_orm_declare_the_same_tables` (this test found it) |
| BUG-020 | Alembic's `asyncio.run()` raises inside FastAPI's already-running lifespan loop, so migrating at startup crashed | High | `adapters/db/bootstrap.py` | Fixed — `run_migrations_async` runs it on a worker thread | `tests/unit/test_api.py` (17 tests), `make preflight` |
| BUG-021 | No lease: a worker crashing mid-send stranded the row in `SENDING` forever | High | `adapters/db/repositories.py`, `orm.py` | Fixed — `claimed_at` column plus `requeue_expired(lease_minutes)`, migration `a1b2c3d4e5f6` | `run_worker_loop` reclaim path; `test_migrations_and_orm_declare_the_same_columns` |
| BUG-022 | No retry: `FAILED` was terminal, `retry_count` was never incremented, and `DEAD` was never reached | High | `worker/outbox_worker.py`, repositories | Fixed — `FAILED` requeues and increments; `DEAD` at `max_attempts`; `ConfigError` is `DEAD` immediately | `test_transport_recovers_within_retry_budget`, `test_misconfigured_transport_is_dead_immediately` |
| BUG-023 | Two migrations' `downgrade()` bodies dropped and recreated indexes on `traces` and `outbox`, tables they never touched | Medium | `migrations/versions/` | Fixed — each drops only the column it added | `test_migrations_round_trip` |
| BUG-024 | `record_edit` wrote `tenant_id=""` | Low | `adapters/db/repositories.py` | Fixed — looked up from the review | code review |

### Observability and accountability

| ID | Problem | Severity | File | Status | Verification |
|---|---|---|---|---|---|
| BUG-025 | Every trace stage event persisted `payload={}`; a trace recorded timings and nothing about what any stage decided, while the docs claimed full reconstructability | High | `pipeline/orchestrator.py` | Fixed — seven stages write real payloads: intent and source, chunk ids and scores, citations, per-validator results, policy violations, per-gate outcomes, and the full signal/threshold breakdown | `test_trace_events_carry_real_payloads` |
| BUG-026 | The `operator` and `reason` accepted by every review endpoint were discarded, and the `audit` table was never written to — while the console told operators "this reason will be logged in the audit trail" | High | `services/review/service.py` | Fixed — `SQLiteAuditRepository` records every transition | `test_human_review_approve_then_deliver` |
| BUG-027 | Claude was priced `0.0 / 0.0`, so every failover to Claude recorded $0 and the cost ledger understated spend | Medium | `config/providers.yaml` | Fixed — real prices | `test_every_provider_is_priced`, `test_failover_to_claude_records_a_nonzero_cost` |
| BUG-028 | Prices were duplicated in each adapter's `capabilities`, so config and code could disagree about what a call cost | Medium | `adapters/llm/*.py`, `ports/llm.py` | Fixed — removed from the port and the adapters; `services/cost` is the only source | `tests/unit/test_budget.py` |
| BUG-029 | The `budgets` block was read by no code | Medium | `services/cost/service.py` | Implemented — `check_daily_budget`, `max_calls_per_message`, 80% warning. **Not yet called per message** — see §3 | `tests/unit/test_budget.py` (8 tests) |
| BUG-030 | The console reported "All 7 Veto Gates Passed" whenever the gate list was empty — i.e. when the gates had not run | Medium | `dashboard/app.py` | Fixed — distinguishes "all N passed" from "not recorded" | code review |

### Integrations

| ID | Problem | Severity | File | Status | Verification |
|---|---|---|---|---|---|
| FUNC-001 | No real delivery adapter existed; the worker reused `send_email_alert` from the alerting service to send customer replies | High | `adapters/channels/smtp_email.py` | Implemented — a real SMTP channel returning a `DeliveryReceipt` or raising a classified error | `test_clean_auto_send_reaches_delivery`; live SMTP failure-classification check |
| FUNC-002 | The Qdrant adapter discarded `doc_type`, `product_tier`, `version` and `source_path` on ingest, created payload indexes on every call, and returned a `sufficient` verdict the service immediately overwrote | Medium | `adapters/vector/qdrant_store.py` | Fixed — stores what it reports, indexes once, reports observations not verdicts | `make ingest` (42 docs / 178 chunks), `make preflight` |
| FUNC-003 | The Gmail poller never stripped quoted reply history, though `InboundMessage` documents `body_text` as "quoted replies stripped"; every reply re-fed the whole thread into retrieval and the prompt | Medium | `adapters/channels/gmail_poller.py` | Implemented — `strip_quoted_reply` | `tests/unit/test_gmail_poller.py` |
| FUNC-004 | The poller built a random `message_id` into its dedupe key, so redelivery could never be detected | High | `adapters/channels/gmail_poller.py` | Fixed — keyed on the RFC-822 `Message-ID` | `tests/unit/test_gmail_poller.py` |
| FUNC-005 | No SLA sweeper existed | Medium | `services/sla/service.py`, `scripts/start_sla_sweeper.py` | Implemented | `test_sla_breach_escalates_an_overdue_review` |

### Architecture and cleanup

| ID | Problem | Severity | File | Status | Verification |
|---|---|---|---|---|---|
| CLEAN-001 | `adapters/channels/telegram.py` could not be imported: `Channel.TELEGRAM` and `DeliveryReceipt` do not exist | Medium | — | Deleted | `tests/unit/test_imports.py` |
| CLEAN-002 | The pipeline imported `QdrantVectorStore` and `ProviderRouter` directly, making the stated hexagonal architecture untrue | High | `pipeline/orchestrator.py` | Fixed — ports only; adapters arrive via `Deps` | `lint-imports` contract "the pipeline depends on ports, never on adapters" |
| CLEAN-003 | `ports/vector_store.py` imported `DocumentChunk` from `services/`, inverting the dependency | Medium | `domain/models.py`, `ports/` | Fixed — `DocumentChunk` moved to the domain | `lint-imports` contract "ports depend only on the domain" |
| CLEAN-004 | Only one boundary contract existed, and it did not cover `pipeline`, `ports` or `domain` | Medium | `pyproject.toml` | Fixed — 4 contracts | `lint-imports`: 4 kept, 0 broken |
| CLEAN-005 | `src/taskflow/ml/` was an empty package; `scikit-learn`, `joblib`, `numpy`, `sentence-transformers` were declared and unused | Medium | — | Deleted, with the dependencies | `uv sync`; `pytest` |
| CLEAN-006 | `ports/cache.py` and `ports/event_bus.py` had zero implementations and zero references; `adapters/{bus,cache,resilience}/` and `dashboard/pages/` were empty packages | Low | — | Deleted | grep: 0 usages |
| CLEAN-007 | `jinja2`, `python-multipart`, `tenacity`, `matplotlib`, `google-api-python-client`, `google-auth-oauthlib` were declared and unused | Low | `pyproject.toml` | Removed | `pytest`, `make preflight` |
| CLEAN-008 | `scripts/verify_system.py` faked LLM responses inline and referenced `Intent.DISPUTE`, which does not exist — it crashed on the offline path it existed to demonstrate | High | — | Deleted; replaced by `scripts/preflight.py`, which checks only real state | `make preflight` exits 0 |
| CLEAN-009 | Makefile targets referenced `taskflow.api.main`, `scripts/generate_kb.py`, `scripts/preflight.py`, `taskflow.ml.*`, `src/taskflow/dashboard/app.py` and `data/snapshots/demo_seed.db` — none of which existed | Medium | `Makefile` | Rewritten — every target resolves | each target run |
| CLEAN-010 | `run_scenario.py` took `--text` while the Makefile called it with `--scenario` and `--all` | Medium | `scripts/run_scenario.py` | Fixed — all three supported, with six named scenarios | `make scenarios` |
| CLEAN-011 | Every entrypoint wired its own dependencies and hardcoded the database URL, ignoring `settings.database_url` and skipping migrations | Medium | `bootstrap.py` | Fixed — one composition root used by API, worker, poller, sweeper, scripts and console | `make preflight` |
| CLEAN-012 | `tests/fixtures/fakes.py::FakeVectorStore` implemented `hybrid_search`/`count`; the port defines `search`/`ingest`/`setup_collection`. It was unusable, and a test written against it would have proved nothing | Medium | `tests/fixtures/fakes.py` | Rewritten — every double matches its port | in use across the suite |
| CLEAN-013 | `tests/pipeline/` imported mocks from `tests/unit/test_providers.py` | Low | `tests/` | Fixed — shared doubles live in `tests/fixtures/` | `pytest` |
| CLEAN-014 | `asyncio_wrap`, a no-op coroutine wrapper, existed to make a synchronous call look asynchronous | Low | `pipeline/orchestrator.py` | Deleted | code review |
| CLEAN-015 | `PipelineState.thresholds` was declared "Expected to be populated inside pipeline" and never was | Low | `pipeline/state.py` | Deleted | `mypy` |
| CLEAN-016 | `settings.teams_webhook_url` and `alerts.channel: teams` were configured; no code sends to Teams | Low | `config/settings.py`, `.env.example` | Removed | grep: 0 usages |
| CLEAN-017 | `CLAUDE.md` stated "Phase P0 — nothing implemented except `domain/` and `ports/`", and the adapter rules required a `@circuit_breaker` decorator that does not exist | Medium | `CLAUDE.md`, `.claude/rules/` | Rewritten to match the code | review |
| CLEAN-018 | `README.md` described an empty project kit — "green on an empty project", "P1 is mostly persistence" | Medium | `README.md` | Rewritten for the built system | review |

### Quality gates

| ID | Problem | Severity | Status | Verification |
|---|---|---|---|---|
| QG-001 | 24 ruff findings | Medium | Fixed | `ruff check src tests scripts dashboard` — clean |
| QG-002 | 9 mypy errors across 5 files | Medium | Fixed | `mypy src/` — no issues, 76 files |
| QG-003 | `disallow_untyped_defs` covered only `domain`, `services`, `ports` | Low | Widened to include `pipeline`, `worker`, `config` | `mypy src/` |
| QG-004 | `tests/e2e/` was empty | High | 14 scenarios added | `pytest tests/e2e -q` |

---

## 3. Remaining Limitations

Genuine, deliberate, and not presented as implemented.

| Limitation | Detail |
|---|---|
| **Credential rotation outstanding** | See §4. The only item on this page that needs a human. |
| **Budget enforcement is not per-message** | `services/cost.check_daily_budget` is implemented, tested, and called by nothing in the request path. Wiring it needs a daily-spend aggregate the trace store does not index today. Spend is recorded per call and surfaced at `GET /metrics`. |
| **Grounding is word overlap, not entailment** | The weakest signal in the score. On live traffic it reads 0.23–0.69 even for drafts that are genuinely grounded, which drags legitimate replies toward review. An entailment check is the highest-value upgrade. |
| **Single tenant** | `tenant_id` is threaded through every row and every retrieval filter, but per-tenant keys, corpora and queues are not built. |
| **PII redaction is regex** | Card numbers, SSNs, `sk_`/`pk_`/Bearer tokens. Not a trained NER model. A name or address in free text is not redacted. |
| **Email only** | `Channel.WEBCHAT` has no transport. Telegram is outbound alerting only. |
| **No spam filter, no FAQ fast path** | `RouteAction.REJECTED_SPAM` and `TEMPLATE_SENT` are defined and unreachable; the `spam:` and `fastpath:` config blocks are unused. Retained deliberately — see BRD §5.3. |
| **Local model quality** | In `local_only` mode `qwen3:8b` sometimes returns empty content for structured output, which correctly degrades to the holding template and human review. The path is sound; the local model is weak. |
| **Not load-tested** | Design point is roughly one message every few seconds. SQLite WAL with a 5 s busy timeout. |
| **Not verified** | The Gmail IMAP poller's live fetch loop was not exercised against a real mailbox during this work. Its message construction, dedupe key and quote stripping are unit-tested; the IMAP conversation itself is not. |

---

## 3a. Closed — the sufficiency check now discriminates

**The finding.** `scripts/eval_retrieval.py` was rewritten to report discrimination
rather than a bare gap rate, and it showed a 0.00% gap rate that read as a perfect
result and was the opposite: with `min_top_score = 0.35` sitting below every score the
system produced, **every** retrieval was declared sufficient, including those that
surfaced no expected document. `retrieval_relevance` was derived from that boolean, so
0.20 of every quality score was a constant.

**Why it could not be fixed by moving the number.** The score being thresholded was
Qdrant's RRF fusion score, which encodes rank position only. Its top value is identical
whether the best candidate was a perfect answer or the least bad of twenty irrelevant
ones — in embedded Qdrant it takes one of a handful of values (0.500, 0.833, 1.000).
No threshold on a rank score can separate a good retrieval from a bad one, so tuning it
would have improved the printed metric without improving a single routing decision.

**The fix, as the BRD §5.3 trigger anticipated.** A cross-encoder reranker
(`adapters/rerank/cross_encoder.py`, behind a `ports.reranker.Reranker` Protocol) scores
the query and each fused candidate together and emits a calibrated 0-1 relevance.
`ScoredChunk.final_score` prefers it, so sufficiency and `retrieval_relevance` now read a
number that carries magnitude. `retrieval_relevance` is that score directly, not a
boolean derived from it.

Measured over the same 351 queries, before and after:

| Metric | Before | After |
|---|---|---|
| Recall@1 | 66.10% | **69.23%** |
| Recall@3 | 86.04% | **90.31%** |
| Recall@5 | 88.89% | **92.88%** |
| MRR@10 | 0.7588 | **0.7909** |
| tier-3 Recall@5 | 78.9% | **86.0%** |
| tier-4 Recall@5 | 71.4% | **88.1%** |
| Median separation (hit − miss) | +0.208 | +0.222 |
| Declared sufficient **but wrong** | 39 | **13** |
| False-confidence rate | 11.11% | **3.70%** |
| Gap rate | 0.00% | 29.63% |
| Average latency | 43.6 ms | 1106.7 ms |

Reproduce either column with `make eval` / `uv run python scripts/eval_retrieval.py
--no-rerank`.

**Read the last two rows carefully.** The gap rate rising off zero is the finding being
fixed, not a new regression: the check can now fail, which is the whole point of having
it. And the latency is a real cost — a local cross-encoder scoring 20 passages, paid once
per message on a pipeline designed for roughly one message every few seconds.

**`min_top_score` is now 0.55, and the choice is published rather than asserted.** The
eval prints a trade-off table (`docs/metrics/retrieval.md`) giving, for each candidate
floor, the share of good retrievals it would escalate against the share of bad ones it
would let through. 0.55 sits between the two medians (0.956 hit, 0.734 miss). Moving it
is a routing decision with a measured cost, and the table is what that decision should be
made from.

**Still open:** `min_support_count = 2` at `min_support_score = 0.20` was calibrated
against RRF scores, where nearly every chunk cleared the bar. Against calibrated relevance
it means "two independently relevant passages", which is a materially stricter bar that a
single-passage answer cannot meet. The measured median support count is 4 of 5, so it
rarely binds today — but it is a threshold whose meaning changed underneath it, and it
deserves the same trade-off treatment before anyone leans on it.

## 4. Action Required From a Human

**Revoke the exposed Gmail app password.**

`.env.example` was tracked in git and contained a working-looking Gmail address and
app password. The file has been scrubbed, but **the credential remains in git history
at commit `52dae4b` and stays valid until it is revoked.** Removing a secret from the
working tree does not invalidate it.

1. Revoke it at <https://myaccount.google.com/apppasswords>.
2. Issue a replacement and put it in `.env` only — that file is git-ignored.
3. Decide separately whether to rewrite history. Revocation is what closes the exposure;
   history rewriting only removes the record.

This is the one item in this document that cannot be closed from inside the repository.
