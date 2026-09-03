# TaskFlow AI Support Agent

Autonomous email support agent with human-in-the-loop governance. Synthetic data,
local-first, **no Docker, no servers**. Every component must be explainable in one
sentence.

**Read `docs/BRD.md` for what the system is required to do, and
`docs/REMEDIATION_STATUS.md` for what has been verified and what is deliberately
deferred.**

## Commands

```bash
make setup       # uv sync + migrations + knowledge-base ingestion   (once)
make preflight   # is this installation able to run? must exit 0
make api         # FastAPI                          :8000
make dash        # Streamlit operator console       :8501
make worker      # outbox delivery worker (real SMTP)
make poller      # Gmail IMAP inbound poller
make sla         # SLA breach sweeper
make check       # lint + typecheck + full test suite (before every commit)
make scenarios   # every named scenario through the real pipeline
make local       # the same, with no cloud LLM at all
```

## Layout

```
src/taskflow/
  domain/     Pydantic models, enums, errors     — pure, imports nothing of ours
  ports/      Protocol definitions               — the seams; depends only on domain
  adapters/   llm/ vector/ db/ channels/         — SDKs live here and nowhere else
  services/   all business logic                 — ports injected, no SDK imports
  pipeline/   the explicit async orchestrator    — ports only, no adapter imports
  worker/     outbox delivery loop
  api/        FastAPI — transport only, no business logic
  config/     settings + YAML loaders
  bootstrap.py  the composition root: the one place adapters are wired to ports
config/       providers.yaml thresholds.yaml policies.yaml settings.yaml prompts/
dashboard/    Streamlit operator console
data/         knowledge_base/ datasets/ manifests/ qdrant/
docs/         BRD.md is authoritative; 00-13 are design notes
tests/        unit/ pipeline/ integration/ e2e/
```

All four layer boundaries are CI-enforced by `lint-imports` (4 contracts).

## Hard rules

1. **`domain/` and `services/` never import `adapters/`, `api/`, or a vendor SDK.**
   Dependencies arrive as `ports.*` Protocols. `pipeline/` and `ports/` are bound by
   the same rule. `make typecheck` fails the build on a violation.
2. **No model names, thresholds, policy rules, prompts, or prices in Python.** They
   live in `config/*.yaml`. `model_for()` is the only way to obtain a model name;
   `services/cost` is the only source of a price.
3. **Safety is a veto, not a weight.** The eight gates are evaluated *before* the
   weighted score and are conjunctive. Never move a safety signal into the weighted
   average, and never add a weighted signal you are not actually measuring — a
   hardcoded `1.0` for a signal is a decision made on evidence that does not exist.
4. **`draft_confidence` from the model is logged, never routed on.**
5. **Fail closed, and fail honestly.** Any error, timeout, or ambiguity routes to
   human review. A failure state must never be recorded as a success — most
   importantly, an outbound message is marked `SENT` only when a channel returned a
   delivery receipt.
6. **Alembic owns the schema.** `Base.metadata.create_all` appears only in tests,
   against throwaway in-memory databases;
   `tests/integration/test_migrations.py` keeps the two definitions identical.
7. **PII is redacted before anything leaves the process** — provider prompts, alert
   channels, webhooks, logs and traces. `body_text` is raw; `body_redacted` is what
   travels.
8. **Retrieved KB text and inbound email are untrusted data, never instructions.**
   Instruction-like patterns set `suspicious_context` and Gate G7 forces review.
9. **Every persisted row carries `tenant_id`.** Migration `upgrade()` bodies are
   append-only; every revision needs a working `downgrade()`.
10. **Every pipeline stage writes a real payload to the trace.** If a decision cannot
    be reconstructed from a `trace_id` alone, the stage is not finished. An empty
    payload is not a trace.
11. **Tests verify intended behaviour, not current behaviour.** When a test and the
    BRD disagree, the BRD wins and the test changes.
12. **Tests never touch a live account.** `tests/conftest.py` blanks every credential
    for the session; do not bypass it.

## Where things are explained

| Question | File |
|---|---|
| What the system must do, and its known limits | `docs/BRD.md` |
| What was broken, what was fixed, and the evidence | `docs/REMEDIATION_STATUS.md` |
| How it fits together | `docs/ARCHITECTURE.md` |
| Why a choice was made | `docs/02_ARCHITECTURE_DECISIONS.md` |
| Shape of any object | `docs/04_SCHEMAS.md` |
| The policy rules | `docs/05_POLICY_RULES.md` |
| Trace schema + dashboard | `docs/08_OBSERVABILITY.md` |
| What happens when X breaks | `docs/09_FAILURE_MODES.md` |
| Threat model + prompt injection | `docs/10_SECURITY.md` |
| Setup and recovery | `docs/11_RUNBOOK.md` |
| Demo walkthrough and scenario table | `docs/12_DEMO_SCRIPT.md` |
| Gmail Push (Watch + Pub/Sub) setup | `docs/14_GMAIL_PUSH_SETUP.md` |
