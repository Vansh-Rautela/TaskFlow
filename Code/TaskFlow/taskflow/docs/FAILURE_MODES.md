# 09 — Failure Modes

**Default posture everywhere: fail closed to human review. Never fail open to auto-send.**

Every row here is a test in `tests/unit` or `tests/integration`.

| Failure | Detection | Fallback | Retry | Customer sees | Escalation |
|---|---|---|---|---|---|
| Claude unavailable | transport error / 5xx | fall through to Ollama | 2× backoff then fall through | nothing | alert on failover |
| Ollama not running | connection refused | if Claude is up, no impact | none | nothing | alert; health tile red |
| **Both providers down** | both adapters fail | human review, `all_providers_failed` | message stays queued | nothing | alert, high severity |
| LLM timeout | per-call deadline (30s cloud / 120s local) | treat as provider error | 1 retry then fall through | nothing | alert if rate > 10% |
| Claude refusal stop reason | `stop_reason == "refusal"` | `SchemaError` → next provider | none | nothing | logged with the message id |
| max_tokens truncation | `stop_reason == "max_tokens"` | `SchemaError` → next provider | none | nothing | logged; consider raising the limit |
| Qdrant unreadable | breaker on the adapter | fast path only; everything else human | breaker 3 fails / 30 s reset | nothing | alert |
| SQLite locked | `OperationalError` | `busy_timeout` waits; then retry the transaction | 3× | nothing | alert if persistent |
| Gmail unavailable | API error on poll | poller backs off; web chat unaffected | exponential to 5 min | nothing | alert after 3 consecutive |
| **Gmail token expired** | 401 on refresh | email ingestion stops | none — needs a human | nothing | alert + runbook link; **preflight catches it first** |
| Teams webhook fails | non-2xx | fallback alerter: `alerts` table + email | 1 retry | nothing | visible on dashboard |
| Classifier artifact missing | load fails at startup | **the app refuses to start** | none | nothing | loud startup failure — never silently degrade to a keyword stub |
| Unknown intent / abstain | max prob < τ | gate G5 → human review | none | nothing | queued with SLA |
| Critical policy violation | deterministic rule match | gate G1 → human review with rule id | none | nothing | queued; alert if severity critical |
| PII in draft | `RegexPIIDetector` (fixed formats) or the injected `PresidioDetector` (adds named entities) — see `ports.pii` | gate G2 → human review | none | nothing | logged with match types, never the values |
| **Own product name flagged as PII** | `PresidioDetector`'s PERSON model misreads a known proper noun (measured: "AcmeFlow Workspace" at 0.85 confidence — appears in nearly every draft) | `config/settings.yaml`'s `pii.allow_list` suppresses it by exact match | none — add the phrase to the list | nothing (would otherwise be gate G2 on close to every auto-send) | if G2 starts firing repeatedly on the same clean phrase, this is why |
| Unresolvable citation | set difference | gate G3 → human review | none | nothing | logged |
| Insufficient retrieval | sufficiency check false | skip drafting → human + `retrieval_gap` | none (no loop) | nothing | gap analyzer |
| Suspicious context | injection patterns in chunks | gate G7 → human review | none | nothing | alert — this is a security event |
| Validator error/timeout | exception or deadline | `errored=True` → gate G6 | none | nothing | logged with the exception |
| Human reviewer unavailable | SLA deadline passes | state → escalated | none | nothing | alert |
| Duplicate message | UNIQUE violation on `inbox.dedupe_key` | drop + `duplicate_dropped` event | none | nothing | counter on health page |
| Delivery failure | send API error | outbox → failed, retry_count++ | 3× backoff → dead | nothing | alert on dead-letter |
| Worker crash mid-message | lease expires (`locked_until`) | next loop reclaims the row | attempts++ | nothing | **exactly-once still holds** via the outbox idempotency key |
| Daily cost cap hit | cost service | template mode; non-FAQ → human | none | nothing | alert at 80% and 100% |

## Degraded-mode matrix

| Breaker open | System behaviour |
|---|---|
| `claude` | all drafting on Ollama; slower; more escalations; banner on the dashboard |
| `ollama` | cloud-only; if Claude also fails, everything escalates |
| `qdrant` | fast path and spam filter still work; everything else escalates with a clear reason |
| `gmail_send` | outbox retains messages; nothing is lost; retries on recovery |

## The drill to rehearse

1. `ollama stop` and break the Claude key → send a message → expect human review with
   `all_providers_failed` plus an alert.
2. Restore Ollama → next message drafts locally, breaker closes.
3. Kill the API process mid-message → restart → the lease expires, the row is reclaimed,
   the customer receives **exactly one** reply.
