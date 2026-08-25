# TaskFlow AI Support Agent — Final Architecture

```mermaid
flowchart TB
    %% ============================================================
    %%  EXTERNAL ACTORS & SYSTEMS
    %% ============================================================

    ACT_CUST["Customer (email / web chat / console)"]

    ES_OR["OpenRouter API (gpt-4o-mini)"]
    ES_CL["Anthropic Claude API (haiku)"]
    ES_OL["Ollama - local (qwen2.5:7b)"]

    ES_QDR["Embedded Qdrant (dense BGE + BM25 + RRF)"]
    ES_SQL["SQLite WAL (state + traces + queues)"]
    ES_GM["Gmail API (poll + SMTP send)"]
    ES_TEAMS["Teams Incoming Webhook"]
    ES_SLACK["Slack Webhook"]
    ES_TG["Telegram Bot API"]
    ES_WEBHOOK["Generic HTTP Webhook"]

    %% ============================================================
    %%  PROCESS 1 — FastAPI Server (:8000)
    %% ============================================================
    subgraph P1["Runtime Process: FastAPI API Server - port 8000"]
        direction TB

        %% --- API layer ---
        subgraph API["api/app.py - FastAPI Entry Point & REST Surface"]
            API_INBOX["POST /messages - ingest InboundMessage"]
            API_HEALTH["GET /healthz"]
            API_TRACES["GET /traces & /traces/{id} - trace inspection"]
            API_REVIEWS["GET /reviews & POST /reviews/{id}/{approve|edit|reject}"]
            API_METRICS["GET /metrics - cost, auto-send rate, volume"]
        end

        %% --- Lifespan DI wiring ---
        subgraph DI["lifespan() - dependency injection wiring"]
            DI_DB["build_engine() + build_session_factory()"]
            DI_REPOS["SQLiteTraceRepo, SQLiteReviewRepo, SQLiteOutboxRepo, SQLiteInboxRepo"]
            DI_LLM["ProviderRouter(openrouter, claude, ollama) - priority from providers.yaml"]
            DI_VEC["QdrantVectorStore - embedded, FastEmbed"]
        end

        %% --- Pipeline orchestrator ---
        subgraph PIPE["pipeline/orchestrator.py - run_pipeline() - explicit async, no framework"]
            direction LR

            P0["P0: Init & Preprocess<br/>preprocess_message_text() - PII regex redaction<br/>trace_repo.start() begins Trace"]

            P1S["P1: Classify Intent<br/>classify_intent() - LLM structured output ClassificationOutput<br/>keyword regex fallback on AllProvidersFailed"]

            P2S["P2: Retrieve Context<br/>retrieve_context() - build_query() alias expansion<br/>vector_store.search() hybrid dense+sparse RRF<br/>evaluate_sufficiency() + detect_context_injection()"]

            P3S["P3: Draft Response<br/>generate_draft() - LLM Router -> DraftOutput JSON<br/>(response_text, citations, tone, complexity)<br/>safe template fallback on provider failure"]

            P4S["P4: Validate (concurrent via asyncio.gather)<br/>pii_leak validator<br/>citations validator<br/>grounding validator (word-overlap ratio)"]

            P5S["P5: Evaluate 7 Conjunctive Gates G1-G7<br/>G1 policy, G2 PII, G3 citations,<br/>G4 intent allows auto-send, G5 abstain,<br/>G6 validators healthy, G7 no context injection<br/>ANY gate fail -> HUMAN_REVIEW"]

            P6S["P6: Score & Decide<br/>compute() weighted quality score (5 signals, config weights)<br/>decide() - score vs per-intent threshold from thresholds.yaml<br/>AUTO_SEND or HUMAN_REVIEW"]

            P7S["P7: Enqueue & Finalize<br/>AUTO_SEND -> outbox_repo.enqueue() (idempotency_key for send-once)<br/>HUMAN_REVIEW -> review_repo.create() (SLA deadline, optimistic lock)<br/>trace_repo.update() + finish()"]
        end

        %% --- Services layer ---
        subgraph SERVICES["services/ - ALL business logic - pure, ports injected"]
            SVC_PRE["preprocess.service - PII redaction (regex)"]
            SVC_CLS["classify.service - LLM intent + keyword fallback"]
            SVC_RET["retrieve.service - hybrid RRF, sufficiency, injection<br/>chunking.py, query_builder.py, sufficiency.py, injection.py"]
            SVC_DRF["draft.service - LLM grounded drafting with citations"]
            SVC_VAL["validate.runner - concurrent validators<br/>citations.py, grounding.py, pii_leak.py"]
            SVC_CON["confidence.gates - 7 conjunctive veto gates<br/>confidence.scorer - weighted quality score"]
            SVC_REW["review.service - pending, approve, edit, reject, escalate"]
            SVC_ALT["alert.service - email SMTP, telegram, slack, webhook"]
            SVC_COS["cost.service - token-to-USD pricing"]

            SVC_PRE --> SVC_CLS
            SVC_CLS --> SVC_RET
            SVC_RET --> SVC_DRF
            SVC_DRF --> SVC_VAL
            SVC_VAL --> SVC_CON
            SVC_CON --> SVC_REW
            SVC_REW --> SVC_ALT
        end

        %% --- Ports (Protocols) ---
        subgraph PORTS["ports/ - Protocol definitions - the seams"]
            PORT_LLM["ports/llm.py - LLMProvider, LLMRouter protocols"]
            PORT_VEC["ports/vector_store.py - VectorStore protocol"]
            PORT_REPO["ports/repositories.py - TraceRepository, ReviewRepository, OutboxRepository, ProcessedMessageRepository"]
            PORT_CACHE["ports/cache.py - Cache protocol (stub)"]
            PORT_CH["ports/channel.py - ChannelConnector protocol (stub)"]
            PORT_BUS["ports/event_bus.py - EventBus protocol (stub)"]
        end

        %% --- Adapters (vendor SDKs only) ---
        subgraph ADAPTERS["adapters/ - vendor SDKs ONLY - the implementation layer"]
            AD_APM["adapters/llm/openrouter.py - OpenRouterProvider (httpx)"]
            AD_CLD["adapters/llm/claude.py - ClaudeProvider (anthropic SDK)"]
            AD_OLA["adapters/llm/ollama.py - OllamaProvider (ollama SDK)"]
            AD_RTR["adapters/llm/router.py - ProviderRouter (failover, cost tracking)"]
            AD_QDR["adapters/vector/qdrant_store.py - QdrantVectorStore (qdrant-client, fastembed)"]
            AD_DBE["adapters/db/engine.py - build_engine (SQLAlchemy async + WAL pragmas)"]
            AD_DBD["adapters/db/orm.py - Base, InboxRow, TraceRow, ReviewRow, OutboxRow, etc."]
            AD_DBR["adapters/db/repositories.py - SQLiteTraceRepository, SQLiteReviewRepository, etc."]
            AD_CH["adapters/channels/ - ChannelConnector impls (stub)"]
            AD_BUS["adapters/bus/ - EventBus impl (stub)"]
            AD_CACHE["adapters/cache/ - Cache impl (stub)"]
            AD_REZ["adapters/resilience/ - Circuit breakers (stub)"]
        end

        %% --- Domain (pure) ---
        subgraph DOMAIN["domain/ - Pydantic models, enums, errors - pure, no I/O"]
            DOM_MOD["models.py - InboundMessage, Chunk, DraftOutput, ValidatorResult,<br/>ConfidenceBreakdown, RoutingDecision, ReviewItem, OutboxMessage, Trace, LLMCall"]
            DOM_ENU["enums.py - Channel, Intent, RouteAction, GateId, ReviewState, OutboxState, Severity"]
            DOM_ERR["errors.py - TaskFlowError, TransientError, ProviderError, SchemaError,<br/>AllProvidersFailed, RetrievalUnavailable, ConflictError, DuplicateMessage"]
        end
    end

    %% ============================================================
    %%  PROCESS 2 — Streamlit Dashboard (:8501)
    %% ============================================================
    subgraph P2["Runtime Process: Streamlit Dashboard - port 8501"]
        direction TB
        DASH_APP["dashboard/app.py - Operator Console"]
        DASH_T1["Tab 1: Human Review Console (Approve/Edit/Reject)"]
        DASH_T2["Tab 2: Trace Explorer & Log Inspection"]
        DASH_T3["Tab 3: System Health & Metrics (KPIs)"]
        DASH_READ["Reads SQLite read-only (all writes go through API)"]
        DASH_APP --> DASH_T1
        DASH_APP --> DASH_T2
        DASH_APP --> DASH_T3
    end

    %% ============================================================
    %%  PROCESS 3 — Outbox Worker (background)
    %% ============================================================
    subgraph P3["Runtime Process: Outbox Background Worker"]
        direction TB
        WORKER_LOOP["worker/outbox_worker.py - run_worker_loop()"]
        WORKER_CLAIM["Claims QUEUED -> SENDING (atomic via UPDATE)"]
        WORKER_DEL["deliver_message() -> alert.service.send_email_alert()<br/>(Gmail SMTP) or send_custom_webhook_alert()"]
        WORKER_MARK["Marks SENT or FAILED + retry_count"]
        WORKER_LOOP --> WORKER_CLAIM
        WORKER_CLAIM --> WORKER_DEL
        WORKER_DEL --> WORKER_MARK
    end

    %% ============================================================
    %%  PROCESS 4 — Operator Scripts (CLI / manual)
    %% ============================================================
    subgraph P4["Operator Scripts (scripts/ - one-off tooling)"]
        SCR_SCEN["run_scenario.py - single message through pipeline"]
        SCR_VERIFY["verify_system.py - 3-query live health demo"]
        SCR_LIVE["test_live_sender.py - E2E pipeline + SMTP + Telegram"]
        SCR_EVAL_C["eval_classifier.py - classification metrics"]
        SCR_EVAL_R["eval_retrieval.py - retrieval quality"]
        SCR_EVAL_D["eval_drafting.py - draft quality"]
        SCR_GEN["generate_datasets.py - classifier train/test sets + leakage check"]
        SCR_INGEST["ingest_kb.py - rebuild embedded Qdrant collection"]
        SCR_CHK["check_no_hardcoded_models.py - pre-commit gate"]
        SCR_LEAK["check_leakage.py - dataset leakage check"]
    end

    %% ============================================================
    %%  DATA & CONFIG
    %% ============================================================
    subgraph DATA["data/ & config/ - static & configuration"]
        DATA_KB["data/knowledge_base/ - 42 markdown docs (8 categories)"]
        DATA_DS["data/datasets/ - classifier_train, classifier_validation,<br/>classifier_test, demo_scenarios, adversarial, golden_eval (JSONL)"]
        DATA_QDR["data/qdrant/ - embedded vector store files"]
        DATA_DB["data/taskflow.db - SQLite WAL (main DB)"]
        DATA_SNAP["data/snapshots/ - demo seed DB (ignored in VCS)"]

        CFG_ENV[".env - secrets & paths (pydantic-settings)"]
        CFG_PROV["config/providers.yaml - provider priority + models + pricing"]
        CFG_THRESH["config/thresholds.yaml - confidence weights + per-intent thresholds + gate params"]
        CFG_POL["config/policies.yaml - 10 policy rules (refund ceiling, no feature dates, etc.)"]
        CFG_SET["config/settings.yaml - tenant, retrieval params, spam rules, SLA minutes"]
        CFG_TPL["config/prompts/templates.yaml - fast-path template responses"]
    end

    %% ============================================================
    %%  TEST SUITE
    %% ============================================================
    subgraph TESTS["tests/ - test pyramid"]
        TEST_UNIT["unit/ - Fast, no network/disk. Uses FakeLLMProvider + FakeVectorStore"]
        TEST_PIPE["pipeline/ - orchestrator tests with mocked providers + in-memory SQLite"]
        TEST_INT["integration/ - full pipeline with real Qdrant + SQLite on disk"]
        TEST_E2E["e2e/ - (empty, aspirational) 5 demo scenarios"]
        TEST_FIX["fixtures/ - conftest.py, fakes.py (MockSuccessProvider, FakeLLMProvider, FakeVectorStore)"]
    end

    %% ============================================================
    %%  ML PACKAGE (empty - deferred)
    %% ============================================================
    subgraph ML["ml/ - EMPTY (fine-tuning deferred to post-v1)"]
        ML_INIT["__init__.py only - no train_classifier.py or evaluate.py"]
    end

    %% ============================================================
    %%  DATA FLOWS
    %% ============================================================

    %% Customer -> API
    ACT_CUST -- "POST /messages (InboundMessageRequest)" --> API_INBOX

    %% API -> Pipeline
    API_INBOX --> P0

    %% Pipeline stage flow (LR within PIPE)
    P0 --> P1S
    P1S --> P2S
    P2S --> P3S
    P3S --> P4S
    P4S --> P5S
    P5S --> P6S
    P6S --> P7S

    %% Pipeline -> Services (dashed: service calls)
    P0 -.-> SVC_PRE
    P1S -.-> SVC_CLS
    P2S -.-> SVC_RET
    P3S -.-> SVC_DRF
    P4S -.-> SVC_VAL
    P5S -.-> SVC_CON
    P6S -.-> SVC_CON

    %% Services -> Ports (the seams)
    SVC_CLS -.-> PORT_LLM
    SVC_DRF -.-> PORT_LLM
    SVC_VAL -.-> PORT_LLM
    SVC_RET -.-> PORT_VEC

    %% Adapters -> external systems
    AD_APM -.-> ES_OR
    AD_CLD -.-> ES_CL
    AD_OLA -.-> ES_OL
    AD_RTR -- "priority failover chain" --> AD_APM
    AD_RTR --> AD_CLD
    AD_RTR --> AD_OLA
    AD_QDR --> ES_QDR
    AD_DBE --> ES_SQL
    AD_DBR --> ES_SQL
    SVC_ALT --> ES_GM
    SVC_ALT --> ES_TEAMS
    SVC_ALT --> ES_SLACK
    SVC_ALT --> ES_TG
    SVC_ALT --> ES_WEBHOOK

    %% Pipeline -> Repositories (SQLite)
    P0 -.-> PORT_REPO
    P6S -.-> PORT_REPO
    P7S -.-> PORT_REPO
    PORT_REPO --> AD_DBR

    %% Pipeline -> Vector Store
    PORT_VEC --> AD_QDR

    %% Pipeline -> LLM Router
    PORT_LLM --> AD_RTR

    %% Domain used by all layers
    DOMAIN --> PORTS
    DOMAIN --> ADAPTERS
    DOMAIN --> PIPELINE
    DOMAIN --> SERVICES

    %% FastAPI lifespan wiring
    DI --> API
    DI_DB --> AD_DBE
    DI_REPOS --> AD_DBR
    DI_LLM --> AD_RTR
    DI_VEC --> AD_QDR

    %% Human reviewer -> review endpoints -> review service
    ACT_REVIEWER -- "approve / edit / reject via Dashboard" --> DASH_APP
    DASH_APP --> API_REVIEWS
    API_REVIEWS --> SVC_REW
    SVC_REW --> AD_DBR
    SVC_REW --> WORKER_CLAIM

    %% Human review -> outbox -> worker -> delivery
    P7S -- "HUMAN_REVIEW" --> SVC_REW
    SVC_REW -- "Approve/Edit -> enqueue" --> AD_DBR
    WORKER_CLAIM --> AD_DBR

    %% FastAPI -> Streamlit (read-only)
    API_TRACES --> DASH_T2
    API_METRICS --> DASH_T3
    API_REVIEWS --> DASH_T1

    %% Scripts -> core layers (bypass API)
    SCR_INGEST --> AD_QDR
    SCR_INGEST --> DATA_KB
    SCR_GEN --> DATA_DS
    SCR_EVAL_C --> ML
    SCR_EVAL_R --> AD_QDR
    SCR_EVAL_D --> AD_RTR
    SCR_CHK --> SRC_SRC["src/taskflow/ (source scan)"]

    %% Scripts -> SQLite + LLM
    SCR_VERIFY --> P0
    SCR_LIVE --> P7S

    %% Tests -> core layers
    TEST_UNIT --> DOMAIN
    TEST_UNIT --> PORTS
    TEST_UNIT --> SVC_CON
    TEST_PIPE --> PIPE
    TEST_INT --> PIPE
    TEST_FIX --> TEST_UNIT
    TEST_FIX --> TEST_PIPE

    %% Config -> Settings / Providers
    CFG_ENV --> SETTING["config/settings.py - pydantic-settings<br/>model_for(), provider_priority(), settings()"]
    CFG_PROV --> SETTING
    CFG_THRESH --> SVC_CON
    CFG_POL --> SVC_CON
    CFG_SET --> SVC_RET
    CFG_TPL --> SVC_DRF

    SETTING --> AD_RTR
    SETTING --> SVC_COS

    %% Data -> adapters
    DATA_KB --> SCR_INGEST
    DATA_QDR --> AD_QDR
    DATA_DB --> AD_DBE
    DATA_SNAP --> P4

    %% Style
    classDef external fill:#e1f5fe,stroke:#01579b,stroke-width:2px
    classDef process fill:#f3e5f5,stroke:#4a148c,stroke-width:2px,stroke-dasharray:4,2
    classDef internal fill:#fff3e0,stroke:#e65100,stroke-width:1px
    classDef stub fill:#fafafa,stroke:#9e9e9e,stroke-width:1px,stroke-dasharray:2,2
    classDef config fill:#f1f8e9,stroke:#33691e,stroke-width:1px
    classDef test fill:#fce4ec,stroke:#88174a,stroke-width:1px
    classDef data fill:#e8f5e9,stroke:#1b5e20,stroke-width:1px

    class ACT_CUST,ES_OR,ES_CL,ES_OL,ES_QDR,ES_SQL,ES_GM,ES_TEAMS,ES_SLACK,ES_TG,ES_WEBHOOK external
    class P1,P2,P3,P4 process
    class API,DI,PIPE,SERVICES,PORTS,ADAPTERS,DOMAIN internal
    class DASH_APP,DASH_T1,DASH_T2,DASH_T3,DASH_READ internal
    class WORKER_LOOP,WORKER_CLAIM,WORKER_DEL,WORKER_MARK internal
    class AD_CH,AD_BUS,AD_CACHE,AD_REZ,ML_INIT PORT_CACHE,PORT_CH,PORT_BUS stub
    class CFG_ENV,CFG_PROV,CFG_THRESH,CFG_POL,CFG_SET,CFG_TPL,SETTING config
    class TEST_UNIT,TEST_PIPE,TEST_INT,TEST_E2E,TEST_FIX test
    class DATA_KB,DATA_DS,DATA_QDR,DATA_DB,DATA_SNAP data
```

