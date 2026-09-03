.DEFAULT_GOAL := help
PY := uv run

help:  ## show all targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

# ---------- setup ----------
setup:         ## first-time setup: dependencies, schema, knowledge base
	uv sync && $(PY) alembic upgrade head && $(PY) python scripts/ingest_kb.py

# ---------- run (no docker, no servers) ----------
api:           ## FastAPI on :8000
	$(PY) uvicorn taskflow.api.app:app --reload --port 8000
dash:          ## Streamlit operator console on :8501
	$(PY) streamlit run dashboard/app.py --server.port 8501
worker:        ## outbox delivery worker (real SMTP)
	$(PY) python scripts/start_worker.py
poller:        ## Gmail IMAP inbound poller (start `make api` first — see docs/11_RUNBOOK.md)
	$(PY) python scripts/start_gmail_poller.py
poller-push:   ## Gmail push inbound listener (needs GCP setup — see docs/14_GMAIL_PUSH_SETUP.md)
	$(PY) python scripts/start_gmail_push.py
sla:           ## SLA breach sweeper
	$(PY) python scripts/start_sla_sweeper.py

# ---------- quality ----------
lint:          ## format + autofix
	$(PY) ruff format src tests scripts dashboard && $(PY) ruff check --fix src tests scripts dashboard
typecheck:     ## mypy + architectural import boundaries
	$(PY) mypy src && $(PY) lint-imports
test:          ## unit tests (fast, no network, no disk services)
	$(PY) pytest tests/unit -q
test-int:      ## integration tests (qdrant + sqlite on disk)
	$(PY) pytest tests/integration tests/pipeline -q
test-e2e:      ## end-to-end scenarios
	$(PY) pytest tests/e2e -q
test-all:      ## the whole suite
	$(PY) pytest -q
check: lint typecheck test-all  ## everything — run before every commit

# ---------- data ----------
migrate:       ## apply database migrations
	$(PY) alembic upgrade head
datasets:      ## regenerate classifier train/test splits + check leakage
	$(PY) python scripts/generate_datasets.py && $(PY) python scripts/check_leakage.py
ingest:        ## (re)build the embedded Qdrant collection
	$(PY) python scripts/ingest_kb.py --recreate
eval:          ## retrieval + classifier + drafting evaluation reports
	$(PY) python scripts/eval_retrieval.py && $(PY) python scripts/eval_classifier.py
calibrate:     ## measure whether different confidence weights separate correct/incorrect drafts better
	$(PY) python scripts/calibrate_confidence.py --source golden

# ---------- verification ----------
preflight:     ## is this installation able to run? must exit 0
	$(PY) python scripts/preflight.py
scenario:      ## run one scenario: make scenario S=refund_750
	$(PY) python scripts/run_scenario.py --scenario $(S)
scenarios:     ## run every named scenario against the real pipeline
	$(PY) python scripts/run_scenario.py --all
local:         ## force the offline path (no cloud LLM at all)
	TASKFLOW_LLM_MODE=local_only $(PY) python scripts/run_scenario.py --all
