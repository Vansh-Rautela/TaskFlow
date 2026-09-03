# TaskFlow

**An autonomous email support agent that knows when *not* to trust itself.**

TaskFlow reads a customer support email, classifies it, retrieves grounded context from a knowledge base, drafts a cited reply — and before that reply is ever allowed to leave the process on its own, it has to survive eight independent safety vetoes and clear a quality bar. Fail either, and the draft doesn't get discarded or forced through: it goes to a human, with the full reasoning attached.

No Docker. No hosted services. No server to stand up. Embedded vector search and SQLite run in-process; the only network calls are to an LLM provider, Gmail, and (optionally) an alert channel.

<p>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue">
  <img alt="Tests" src="https://img.shields.io/badge/tests-191%20passing-brightgreen">
  <img alt="Type checked" src="https://img.shields.io/badge/type--checked-mypy-blue">
  <img alt="Architecture" src="https://img.shields.io/badge/architecture-hexagonal-orange">
  <img alt="Deployment" src="https://img.shields.io/badge/deployment-local--first-lightgrey">
</p>

---

## Contents

- [Why this is interesting](#why-this-is-interesting)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [The pipeline](#the-pipeline)
- [The decision engine](#the-decision-engine)
- [Verified, not claimed](#verified-not-claimed)
- [Repository structure](#repository-structure)
- [Running the full stack](#running-the-full-stack)
- [API reference](#api-reference)
- [Testing & quality](#testing--quality)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Roadmap](#roadmap)

---

## Why this is interesting

Most "AI agent" demos show you the happy path: the model writes something plausible, everyone nods. TaskFlow's actual subject is the other path — **the part where the system has to know it's wrong before anyone else finds out.**

- **Safety is a veto, not a weight.** Eight deterministic gates run *before* any score is computed. A draft that would score 0.91 on quality still doesn't send if it promises a refund over the policy ceiling — the gate check physically happens first, in code, not as a tunable weight that a large enough number could someday outvote.
- **Nothing is asserted, everything is measured.** Retrieval quality, classifier accuracy, and confidence-score calibration are all backed by scripts that write a report — not a number someone typed into a README. See [Verified, not claimed](#verified-not-claimed).
- **It fails honestly.** No LLM reachable → falls back to a local model, and says so on the trace. Not enough reviewed data to calibrate the scoring weights → the calibration script says exactly that instead of guessing. A gate can't be silently bypassed by a good enough score, ever.
- **Every decision is reconstructable.** One `trace_id` captures every stage's real output — not a timestamp, the actual payload — so "why did it do that" is always answerable without re-running anything.
- **Genuinely local-first.** Embedded Qdrant (hybrid dense + sparse search, server-side RRF fusion, local cross-encoder reranking) and SQLite run inside the same process. `TASKFLOW_LLM_MODE=local_only` runs the entire pipeline with zero internet access.

---

## Quick start

```bash
git clone https://github.com/Vansh-Rautela/TaskFlow.git
cd TaskFlow/Code/TaskFlow/taskflow

cp .env.example .env               # fill in an LLM provider key at minimum
make setup                         # uv sync + migrate + ingest the knowledge base
make preflight                     # must exit 0
```

```bash
# terminal 1 — start this first, it's the only process that opens the vector store
make api        # FastAPI :8000

# terminal 2
make dash       # Streamlit operator console :8501
```

```bash
# see it decide something, no daemons required
uv run python scripts/run_scenario.py --scenario billing_payment_methods
```

Full command reference, every optional service, and a from-scratch walkthrough are in [Running the full stack](#running-the-full-stack).

---

## Architecture

Strict hexagonal (ports & adapters). Business logic never imports a vendor SDK — dependencies arrive as `Protocol` types and get wired once, at startup, in one composition root. This is enforced at build time, not by convention: `make typecheck` runs `import-linter` and fails the build on a violation.

```
┌─────────────────────────────────────────────────────────────────────┐
│                             Entrypoints                              │
│         FastAPI (:8000)  │  Streamlit Dashboard (:8501)  │  CLI      │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Pipeline Orchestration                        │
│         Sequential execution │ State machine │ Trace logger          │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Ports (Protocols)                           │
│    LLMProvider │ VectorStore │ Repositories │ ChannelConnector │ ... │
└─────────────────────────────────────────────────────────────────────┘
          │                    │                    │            │
          ▼                    ▼                    ▼            ▼
 ┌─────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
 │   LLM Adapters   │ │  Vector Adapter   │ │ Database Adapters │ │ Channel Adapters │
 │ OpenRouter/Claude│ │  Embedded Qdrant  │ │ SQLite / Alembic  │ │ Gmail IMAP/Push  │
 │ Ollama (local)   │ │  Dense + Sparse   │ │ Async SQLAlchemy  │ │ SMTP delivery    │
 └─────────────────┘ └──────────────────┘ └──────────────────┘ └──────────────────┘
```

| Layer | Path | Rule |
|---|---|---|
| **Domain** | `src/taskflow/domain/` | Pure Pydantic models, enums, errors. No dependency on anything else. |
| **Ports** | `src/taskflow/ports/` | `Protocol` contracts. Depends only on the domain. |
| **Services & Pipeline** | `src/taskflow/services/`, `src/taskflow/pipeline/` | All business logic and orchestration. Ports injected, never a vendor SDK. |
| **Adapters** | `src/taskflow/adapters/` | Every SDK (Anthropic, Ollama, Qdrant, SQLAlchemy, Gmail, Presidio) — and nowhere else. |
| **`bootstrap.py`** | `src/taskflow/bootstrap.py` | The composition root. The one place an adapter meets a port. |

---

## The pipeline

One strict sequential path, no event bus, no framework — the order below is the order the code runs in:

```
1 Dedupe → 2 Redact PII → 3 Classify → 4 Retrieve → 5 Draft
  → 6 Validate → 7 Policy → 8 Safety gates → 9 Score → 10 Decide
```

| Stage | What happens |
|---|---|
| **1. Dedupe** | A unique key derived from the message drops a redelivered email before any work starts. |
| **2. Redact PII** | Two detectors, same interface: a dependency-free pattern matcher (cards, SSNs, API keys) always available, and a named-entity model (catches a name, email, or phone number — nothing a fixed pattern could) that production traffic uses by default. |
| **3. Classify** | LLM-based intent classification across 8 categories, with a deterministic keyword fallback if no provider is reachable. |
| **4. Retrieve** | Hybrid dense + BM25 search fused server-side with RRF, re-ranked by a local cross-encoder, checked for sufficiency before anything downstream trusts it. |
| **5. Draft** | An LLM writes a reply grounded in the retrieved passages, with inline citations. No provider reachable → a holding template, explicitly flagged as such. |
| **6. Validate** | Citation resolution, grounding entailment, and PII-leak checks run concurrently, each with its own timeout, failing closed on a crash. |
| **7. Policy** | Deterministic regex rules over both the inbound message and the draft — refund ceilings, legal-threat detection, credential requests. No model involved. |
| **8. Gates** | Eight conjunctive safety vetoes. Any failure routes to human review, full stop. |
| **9. Score** | A weighted quality score, computed only for drafts that already passed every gate. |
| **10. Decide** | Score clears the per-intent threshold → auto-send. Otherwise → human review, with an SLA deadline and an instant alert. |

---

## The decision engine

Two layers, deliberately not one.

### Layer 1 — deterministic veto gates

If **any** gate fails, the message routes to human review — regardless of how high the quality score is.

| Gate | Blocks on |
|---|---|
| **G1** Critical policy violation | A policy rule fires on the draft or inbound message (e.g. a refund promised over the $500 agent ceiling, a legal threat). |
| **G2** PII leakage | The draft contains unredacted PII or a secret pattern. |
| **G3** Unresolved citations | A citation can't be mapped back to a retrieved chunk — or nothing was cited at all despite context being available. |
| **G4** Restricted intent | Intent is `complaint` or `unknown`. |
| **G5** Low classifier confidence | Below the abstain threshold (0.55 default). |
| **G6** Validator error | Any validator crashed or timed out. |
| **G7** Context injection | Retrieved content contains instruction-like patterns. |
| **G8** Fallback template used | No LLM actually wrote this reply. |

### Layer 2 — per-intent quality score

For drafts that pass every gate, five signals combine into one number:

```
score = 0.35·citations + 0.25·grounding + 0.20·retrieval + 0.10·intent + 0.10·tone
```

compared against a bar that scales with the stakes:

| Intent | Threshold | | Intent | Threshold |
|---|---|---|---|---|
| General / Technical / Account | 0.70 | | Billing | 0.80 |
| Feature Request | 0.75 | | Refund | 0.90 |
| Complaint / Unknown | *1.01 — unreachable by design* | | | |

The weights are heuristics, stated as such — `make calibrate` measures whether a different weighting would separate correct drafts from incorrect ones better, over real reviewed outcomes, without ever touching a gate.

---

## Verified, not claimed

Every number below comes from a script that measures it, or a live run this repo's own history can be checked against — not a figure typed into this file.

| | |
|---|---|
| **191 automated tests** passing — unit, integration, pipeline, and end-to-end — none asserting generated prose, all asserting routing decisions and reason codes | `make check` |
| **4/4 architecture contracts held** — domain and services provably never touch a vendor SDK | `make typecheck` (`import-linter`) |
| **Recall@5 92.9%**, MRR@10 0.79, measured over 351 distinct queries with real per-query ground truth, across four difficulty tiers | `scripts/eval_retrieval.py` → `docs/metrics/retrieval.md` |
| **A real inbound email**, sent live to the demo inbox, correctly routed to human review for containing a legal threat — arriving via Gmail Watch + Pub/Sub, not IMAP polling | `docs/14_GMAIL_PUSH_SETUP.md` |
| **A named-entity PII detector's first real-world test caught a real bug** (its own product's name misread as a person) — found, root-caused, and fixed with a config-driven allow-list, not silently shipped | `docs/09_FAILURE_MODES.md` |
| **The confidence calibration script refuses to calibrate** on too little data rather than pretend five weights can be learned from a handful of reviews | `scripts/calibrate_confidence.py` |

---

## Repository structure

<details>
<summary><strong>Click to expand the full tree</strong></summary>

```
taskflow/
├── config/                        # Behavioural configuration (YAML) — never in Python
│   ├── policies.yaml              # Policy detection rules
│   ├── providers.yaml             # LLM provider fallback priority and pricing
│   ├── settings.yaml              # Retrieval sizing, model names, PII, poller intervals
│   ├── thresholds.yaml            # Scoring weights, per-intent thresholds, SLAs
│   └── prompts/                   # System prompt definitions
│
├── src/taskflow/                  # Source package
│   ├── domain/                    # Pure Pydantic models, enums, exceptions
│   ├── ports/                     # Protocol definitions — the seams
│   │   ├── channel.py  llm.py  pii.py  reranker.py  repositories.py  vector_store.py
│   ├── services/                  # All business logic — ports injected, never an SDK
│   │   ├── alert/       classify/    confidence/   cost/       draft/
│   │   ├── policy/      preprocess/  retrieve/     review/     sla/       validate/
│   ├── pipeline/                  # orchestrator.py + state.py — the explicit sequence
│   ├── adapters/                  # Every SDK lives here, and nowhere else
│   │   ├── channels/               # Gmail IMAP poll, Gmail Push (Watch+Pub/Sub), SMTP
│   │   ├── db/                     # Async SQLAlchemy, SQLite WAL, Alembic migrations
│   │   ├── llm/                    # Anthropic, Ollama, OpenRouter + the failover router
│   │   ├── pii/                    # Presidio + spaCy named-entity detector
│   │   ├── rerank/                 # Local cross-encoder reranker
│   │   └── vector/                 # Embedded Qdrant vector store
│   ├── api/app.py                 # FastAPI — transport only, no business logic
│   ├── worker/outbox_worker.py    # Background delivery worker
│   └── bootstrap.py               # Composition root
│
├── dashboard/app.py                # Streamlit operator console
├── scripts/                        # 15 CLI tools — see `make help`
├── tests/                          # unit/ integration/ pipeline/ e2e/
├── data/                           # SQLite DB, embedded vector store, datasets
├── docs/                           # BRD, product spec, architecture, runbooks
└── Makefile                        # Every command below is a one-line wrapper here
```

</details>

Both Gmail inbound adapters post to the API's `/messages` endpoint rather than running the pipeline in-process — the API is the only process allowed to open the embedded vector store, so a second process doing the same crashes with a file-lock error by design. Start `make api` before either.

---

## Running the full stack

### Prerequisites

- Python 3.12 (3.13 unsupported — ML wheel compatibility)
- [`uv`](https://docs.astral.sh/uv/)
- At least one reachable LLM provider: an OpenRouter/Anthropic key, or a local Ollama install

### First-time setup

```bash
cp .env.example .env
```

```ini
TASKFLOW_API_TOKEN=your-secure-api-token
TASKFLOW_LLM_MODE=cloud_first        # or local_only — zero internet, Ollama only
OPENROUTER_API_KEY=your-key
GMAIL_USER=your-support-email@gmail.com    # optional
GMAIL_APP_PASSWORD=your-app-password       # optional
```

```bash
make setup       # uv sync + migrate + ingest the knowledge base
make preflight    # must exit 0
```

### Every service, and what it's for

| Command | Runs | Needs |
|---|---|---|
| `make api` | FastAPI on `:8000` | Start this first, always |
| `make dash` | Streamlit console on `:8501` | — |
| `make worker` | Delivers approved/auto-sent replies over real SMTP | Gmail creds |
| `make poller` | Gmail IMAP polling, checks every 15–30s | Gmail creds |
| `make poller-push` | Gmail Watch + Pub/Sub, near-instant | [one-time GCP setup](docs/14_GMAIL_PUSH_SETUP.md) |
| `make sla` | Auto-escalates review items past their SLA | — |

### Driving it without a daemon

```bash
uv run python scripts/run_scenario.py --text "I was double charged on my invoice."
uv run python scripts/run_scenario.py --scenario refund_750     # see the veto in action
uv run python scripts/run_scenario.py --all                     # every named scenario
make local                                                       # same, zero internet
```

---

## API reference

`http://localhost:8000` — every mutating endpoint requires `Authorization: Bearer $TASKFLOW_API_TOKEN`.

| Method | Endpoint | Does |
|---|---|---|
| `GET` | `/healthz` | Health check, unauthenticated |
| `POST` | `/messages` | Ingest a message, run the full pipeline |
| `GET` | `/traces` · `/traces/{id}` | Recent traces / one full trace |
| `GET` | `/reviews` | Items pending human review |
| `POST` | `/reviews/{id}/approve` · `/edit` · `/reject` | Action a review item |
| `GET` | `/metrics` | Aggregated cost, action mix, provider failovers |

```bash
curl -X POST http://localhost:8000/messages \
  -H "Authorization: Bearer $TASKFLOW_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"message_id":"msg-1","channel":"email","sender":"customer@example.com",
       "subject":"Cancellation","body_text":"I would like to cancel my subscription."}'
```

---

## Testing & quality

```bash
make check        # lint + typecheck + the full suite — everything below, one command
```

| | |
|---|---|
| `make lint` | `ruff format` + `ruff check --fix` |
| `make typecheck` | `mypy` (strict on domain/services/ports/pipeline) + `import-linter` |
| `make test` | Unit only — no network, no disk services, <10s |
| `make test-int` | + embedded Qdrant, SQLite on disk |
| `make test-e2e` | Full pipeline, real repositories, only the LLM/SMTP transports faked |

---

## Configuration

Everything tunable lives in `config/*.yaml` — never as a literal in Python. A hardcoded model name, threshold, or policy rule in `src/` is treated as a bug (`scripts/check_no_hardcoded_models.py` checks for it).

| File | Controls |
|---|---|
| `providers.yaml` | Model priority (`cloud_first`/`local_only`), pricing, budget caps |
| `thresholds.yaml` | Scoring weights, per-intent thresholds, gate parameters, SLAs |
| `policies.yaml` | Business rules — refund ceilings, prohibited patterns |
| `settings.yaml` | Retrieval sizing, model names (dense/sparse/reranker/PII), poller intervals |

---

## Documentation

Start with **[`docs/BRD.md`](docs/BRD.md)** — the business requirements, what the system must do, and its known limits. **[`docs/PRODUCT_SPECIFICATION.md`](docs/PRODUCT_SPECIFICATION.md)** is its technical companion.

<details>
<summary><strong>Full documentation index</strong></summary>

| Doc | Covers |
|---|---|
| `docs/00_PROJECT_CONTEXT.md` | Why this exists, scope, explicit non-goals |
| `docs/01_ARCHITECTURE.md` | Deep dive into architecture and pipeline flow |
| `docs/02_ARCHITECTURE_DECISIONS.md` | ADRs — why each non-obvious call was made |
| `docs/03_IMPLEMENTATION_PLAN.md` | The phased build plan |
| `docs/04_SCHEMAS.md` | Data contracts, enums, domain models |
| `docs/05_POLICY_RULES.md` | Governance and business policy specs |
| `docs/06_DATA_GENERATION.md` | How the synthetic KB and datasets were built |
| `docs/07_EVAL_METHODOLOGY.md` | Classifier, retrieval, drafting evaluation methodology |
| `docs/08_OBSERVABILITY.md` | Trace schema and how the dashboard reads it |
| `docs/09_FAILURE_MODES.md` | What happens when a dependency breaks |
| `docs/10_SECURITY.md` | Threat model, prompt-injection defense, PII policy |
| `docs/11_RUNBOOK.md` | Setup, daily operation, troubleshooting |
| `docs/12_DEMO_SCRIPT.md` | The rehearsed demo walkthrough and scenario table |
| `docs/13_PRODUCTION_READINESS_GAP.md` | What v1 deliberately doesn't do, and what each would cost |
| `docs/14_GMAIL_PUSH_SETUP.md` | Step-by-step GCP setup for push mode |
| `docs/REMEDIATION_STATUS.md` | Audit trail — bugs found and fixed, with evidence |

`BRD.md` and `PRODUCT_SPECIFICATION.md` also export to PDF/DOCX via `docs/_export/export.sh` — the markdown is the source of truth; run it after editing either.

</details>

---

## Roadmap

What's shipped versus what's next — the honest version lives in [`docs/PRODUCT_SPECIFICATION.md` §14](docs/PRODUCT_SPECIFICATION.md#14-future-enhancements) and [`docs/13_PRODUCTION_READINESS_GAP.md`](docs/13_PRODUCTION_READINESS_GAP.md). In short:

**Recently shipped:** Gmail Push (verified live), named-entity PII detection in production, confidence-weight calibration tooling.

**Next up:** calibrating those weights against real reviewed outcomes once enough exist, expanding the knowledge base beyond billing, and fine-tuning a drafting model on the edit history the review console already captures.

**Deliberately not attempted:** multi-tenant enforcement, multi-operator permissions, hosted deployment. Scoped out on purpose for a single-machine POC, not missed by accident — see the docs above for what each would actually cost.
