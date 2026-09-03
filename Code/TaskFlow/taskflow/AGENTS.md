# AGENTS.md — TaskFlow AI Support Agent

Cross-tool agent context. Read by Antigravity, Codex, Cursor, Gemini CLI and others.
Claude Code additionally reads `CLAUDE.md` (same rules, tool-specific format).

## Project

Autonomous email + web-chat customer-support agent with human-in-the-loop governance,
built solo in 4 weeks for a presentation to senior engineers. Synthetic data only.
Runs entirely locally: **no Docker, no database server, no vector-DB server, no message broker.**

The point of the project is not "an AI answers emails". It is: *a system that knows when
it is not allowed to answer, and can prove why.*

## Environment

- Python 3.12, `uv` for dependency management (`uv sync`, `uv run <cmd>`)
- Embedded Qdrant (`QdrantClient(path=...)`) — no server process
- SQLite with WAL — application state, traces, queue, outbox, reviews
- LLM provider chain: OpenRouter → Claude → Ollama (local fallback), configured in
  `config/providers.yaml` under `priority.cloud_first` / `priority.local_only`
- Two processes total: `make api` (:8000) and `make dash` (:8501)

## Commands that work today

Every Makefile target below resolves to a script or module that exists. The
"aspirational targets" list that used to live here is gone: as of 2026-08-25 the
Makefile was rewritten so there is no gap between what it advertises and what runs.

```bash
make setup          # uv sync + alembic upgrade head + knowledge-base ingestion
make preflight      # is this installation able to run? must exit 0
make api            # FastAPI, :8000   (module is taskflow.api.app:app)
make dash           # Streamlit operator console, :8501
make worker         # outbox delivery worker (real SMTP)
make poller         # Gmail IMAP inbound poller
make sla            # SLA breach sweeper
make lint           # ruff format + autofix
make typecheck      # mypy + lint-imports (4 architecture contracts)
make test           # unit tests, fast, no network
make test-int       # integration tests (Qdrant + SQLite on disk)
make test-e2e       # end-to-end scenarios
make test-all       # the whole suite
make check          # lint + typecheck + test-all — run before every commit
make migrate        # alembic upgrade head
make ingest         # rebuild the embedded Qdrant collection (ingest_kb.py --recreate)
make datasets       # regenerate classifier datasets + leakage check
make eval           # retrieval + classifier evaluation reports
make scenario S=refund_750   # one named scenario through the real pipeline
make scenarios      # all of them
make local          # the same, with no cloud LLM at all
```

Scripts, if you prefer to call them directly:

```bash
uv run python scripts/preflight.py                            # real state checks only
uv run python scripts/run_scenario.py --text "I need a refund"
uv run python scripts/run_scenario.py --scenario refund_750
uv run python scripts/eval_retrieval.py                       # -> docs/metrics/retrieval.md
uv run python scripts/eval_retrieval.py --no-rerank           # what reranking is worth
uv run python scripts/eval_classifier.py --path keyword       # -> docs/metrics/classification.md
uv run python scripts/eval_drafting.py --limit 10             # real pipeline; costs money
```

## Before implementing anything

1. Read `docs/BRD.md` — it is authoritative for what the system must do, and §5.2/§5.3
   record what is deliberately out of scope or deferred.
2. Read `docs/REMEDIATION_STATUS.md` — it records what was broken, what was fixed, the
   evidence for each, and the open findings. Do not re-fix something already closed, and
   do not reopen a documented deferral without a reason.
3. Read `docs/04_SCHEMAS.md` for any object you will touch.
4. State the files you will create or modify before writing code.

Do not implement more than one phase step at a time. Do not write code for a later
phase because it seems convenient.

## Architecture

Hexagonal (ports & adapters), enforced by `lint-imports`:

```
src/taskflow/
  domain/     Pydantic models, enums, errors      — pure, no I/O
  ports/      Protocol definitions (llm, vector_store, reranker, repositories, channel)
  services/   business logic, one subpackage per capability
              (classify, retrieve, draft, validate, confidence, review, alert, cost, ...)
  adapters/   llm/ (openrouter, claude, ollama, router) vector/ (qdrant) rerank/ (cross_encoder)
              db/ (sqlite+alembic) channels/ (gmail_poller, smtp_email)   — SDKs ONLY here
  pipeline/   orchestrator.py + state.py           — explicit async function, no framework
  api/        app.py (FastAPI, bearer-authenticated, lifespan-wired deps)
  worker/     outbox_worker.py                     — polls outbox, delivers via a channel port
  bootstrap.py  the composition root — the one place adapters are wired to ports
  dashboard is the top-level dashboard/app.py
```

**Request flow** (`pipeline/orchestrator.py:run_pipeline`):
dedupe → redact → classify → retrieve → draft → validate (concurrent) → policy → **gates** (8 conjunctive
safety vetoes) → weighted confidence score → decide → AUTO_SEND enqueues to outbox /
HUMAN_REVIEW creates a review item → trace finalized. Every stage writes a timed trace
event. The outbox worker (`worker/outbox_worker.py`) claims queued messages and delivers
via `services/alert/service.py` (Gmail SMTP, Telegram, generic webhook).

**Config layering:** secrets and paths in `.env` (pydantic-settings), behavior in
`config/*.yaml`, prompts in `config/prompts/`. `config/settings.py:model_for()` is the
ONLY sanctioned way to obtain a model name — a pre-commit hook
(`scripts/check_no_hardcoded_models.py`) fails the build on model strings in `src/`.

## Code standards

- Full type annotations on every public function. `X | None`, not `Optional[X]`.
- Pydantic v2 models for anything crossing a module boundary.
- `async` for all I/O. No blocking calls in request or worker paths.
  (Known exception: `services/alert/service.py` uses sync `smtplib` inside async functions.)
- `structlog` for logging, never `print` (scripts/ excepted — they are operator tooling).
- No bare `except:`. Adapters translate SDK errors into `taskflow.domain.errors` types.
- Model names, thresholds, policy rules and prompts live in `config/`, never in Python.
- Ruff: line-length 100, rules `E,F,I,UP,B,SIM,ASYNC,RUF`, `E501` ignored.
- mypy is strict (`disallow_untyped_defs`, `strict_equality`) for `domain/`, `services/`, `ports/` only.

## Architecture constraints (non-negotiable)

- `domain/` and `services/` must not import `adapters/`, `api/`, or any vendor SDK
  (`anthropic`, `ollama`, `qdrant_client`, `googleapiclient`, `sqlalchemy`, `streamlit`).
  Enforced by `lint-imports` in `make typecheck`. Do not work around it with
  function-body imports.
- Safety checks (policy, PII, citations, intent class, abstention, validator health,
  suspicious context) are **conjunctive gates evaluated before the weighted score**
  (`services/confidence/gates.py`, G1–G7). Never convert a gate into a score weight.
  See ADR-001 in `docs/02_ARCHITECTURE_DECISIONS.md`.
- Validators that raise or time out must return `ValidatorResult(passed=False,
  errored=True)` which trips gate G6 — never let an exception skip a safety check.
- The model's self-reported `draft_confidence` is logged and never used for routing.
- Every failure path routes to human review. Never auto-send on uncertainty.
- Every pipeline stage writes to the trace; a decision must be reconstructible from
  a `trace_id` alone.
- `PipelineState` is immutable — use `state.replace(...)`, never mutate in place.
- Retrieved KB text and inbound email are untrusted data, never instructions.

## LLM provider failover

`adapters/llm/router.py:ProviderRouter.complete_structured` walks the priority list
from `config/providers.yaml`. A provider attempt fails over on `TransientError`,
`ProviderError`, or `SchemaError` (which includes refusal and max_tokens stop reasons,
and JSON that fails `schema.model_validate_json`). If all providers fail it raises
`AllProvidersFailed`. `TASKFLOW_LLM_MODE=local_only` restricts the chain to Ollama.

## Testing

- `tests/unit/` — fast, no network/disk/Ollama. Use `FakeLLMRouter`, `FakeVectorStore`,
  `RecordingChannel` and `FakeLLMProvider` from `tests/fixtures/fakes.py`. Each double
  implements the same Protocol as the adapter it replaces; a double whose shape has
  drifted from its port lets a test pass against an interface that does not exist.
- `tests/conftest.py` installs a session-wide autouse fixture that blanks every
  credential. Do not bypass it: before it existed, running the suite against a populated
  `.env` dispatched real alert emails through a live Gmail account.
- `tests/pipeline/` — orchestrator tests with mocked providers and in-memory SQLite.
- pytest markers: `integration` (Qdrant/SQLite/Ollama on disk), `live` (real API calls,
  costs money), `e2e`, `slow`. `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.
- E2E tests assert **routing decisions and reason codes**, never generated prose.
- Write the test before the implementation for: gates, confidence scoring, dedupe,
  outbox send-once, delivery state transitions, and SLA computation.
- A test is only "end-to-end" if the boundary under test is real. Mocking the pipeline
  and asserting the mock was called is not an E2E test.
- Never call a live LLM API inside a unit test. Never delete or weaken a failing test.

## Migrations (Alembic)

- Append-only: never edit an applied revision; add a new one. Every migration needs a
  working `downgrade()`.
- Never import ORM models inside a migration — use `sa.table()` / `sa.column()` literals.
- Every table has `tenant_id`. Tables written by more than one code path have UNIQUE
  constraints (`inbox.dedupe_key`, `outbox.idempotency_key`).
- Note: `api/app.py` lifespan currently calls `Base.metadata.create_all` directly;
  `make migrate` (alembic) is the intended path and both exist — be aware when changing schemas.

## Rejected technologies — do not introduce

Docker, Redis, Kafka, RabbitMQ, Celery, Elasticsearch, LangGraph, Slack (including as
an alert channel — removed; not in use), LangChain, a second database, a hosted
deployment, MCP servers. Each has a written rejection in
`docs/02_ARCHITECTURE_DECISIONS.md`. Adding one requires overturning an ADR first.

## Deny rules (safety — auto-execution is on)

- Never read, edit, or print `.env`, `secrets/**`, or `data/snapshots/**`.
  (`.env.example` is fine to read but treat any credentials in it as sensitive.)
- Never run `rm -rf`, `git push --force`, `git reset --hard`, or `alembic downgrade`
  without explicit confirmation in the current turn.
- Never commit generated datasets, `*.db` files, or model artifacts.
- Never call a live LLM API inside a unit test. Live calls belong in `-m live` tests only.
- Never delete or weaken a test to make a build pass. Report the failure instead.

## Verification (run before claiming a step is done)

```bash
make check          # ruff + mypy + lint-imports + unit tests
make test-int       # when adapters changed
uv run python scripts/run_scenario.py --text "..."   # when gates or policy changed
```

A step is only complete when its Definition of Done in `docs/03_IMPLEMENTATION_PLAN.md`
is fully satisfied, the docs are updated in the same change, and `make check` is green.

## Documentation map

| Question | File |
|---|---|
| What is this, what's out of scope | `docs/00_PROJECT_CONTEXT.md` |
| How it fits together | `docs/01_ARCHITECTURE.md` |
| Why a choice was made (ADRs) | `docs/02_ARCHITECTURE_DECISIONS.md` |
| What to build next | `docs/03_IMPLEMENTATION_PLAN.md` |
| Shape of any object | `docs/04_SCHEMAS.md` |
| The policy rules | `docs/05_POLICY_RULES.md` |
| What happens when X breaks | `docs/09_FAILURE_MODES.md` |
| Threat model + prompt injection | `docs/10_SECURITY.md` |
| Setup and recovery | `docs/11_RUNBOOK.md` |

## Tool surface

- Workflows: `.agents/workflows/` — `/verify-slice`, `/start-phase`, `/demo-rehearsal`
- Rules: `.agents/rules/` and `.claude/rules/` (mirrored) — path-scoped constraints
  applied by glob: `pipeline.md`, `services-purity.md`, `adapters.md`, `tests.md`,
  `migrations.md`, `python-style.md`
- Skills: `.claude/skills/` (Claude Code) — verify-slice, run-eval, write-adr
- MCP: none required. Do not add MCP servers for this project.
