# 12 — Demo Script

15 minutes. Rehearse twice, on different days, from a cold start.

`data/datasets/demo_scenarios.jsonl` is the single source of truth for every named
scenario below — `scripts/run_scenario.py --scenario <id>` runs one, `--all` runs
the full set. Each was run against the real pipeline (real providers, real
retrieval, real gates) to confirm the action and reason code shown here; none of
this table is aspirational.

## Pre-flight (before anyone is in the room)

```bash
make preflight     # must exit 0
```

Checks: scoring weights sum to 1.0 · per-intent thresholds configured · API token
set · database migrated and readable · SMTP delivery configured · knowledge base
indexed · at least one LLM provider reachable (OpenRouter, Claude, Ollama). There is
no automated reset script yet — see **Insurance** below for what to do if a run
leaves bad state.

## Running order

| # | Time | Do | Say |
|---|---|---|---|
| 0 | 0:00 | Show the architecture on one slide, then close it | "Eleven components. The interesting one is the decision layer — I'll come back to it." |
| 1 | 1:00 | `make scenario S=billing_payment_methods` → auto-sent with citations | "Classified, retrieved, drafted, validated, cited, sent. Under four seconds, and it only got to send because every gate and the score both agreed." |
| 2 | 3:00 | Open the trace for #1 in the dashboard's Trace Explorer. Walk all stages slowly | "This is the actual product. Every score, every validator, every gate, in one row you can hand to an auditor." |
| 3 | 6:00 | `make scenario S=refund_750` → blocked. Point at the score and gate G1 red | **"The quality score alone would have cleared the threshold. It still didn't send. Safety is a veto, not a weighted average — if policy were a weight instead of a gate, this email goes out."** |
| 4 | 9:00 | Approve it with an edit in Streamlit's Review Queue; show the `EditRecord` row | "Both texts stored. That's the training data for the fine-tuning calibration described in `13_PRODUCTION_READINESS_GAP.md`." |
| 5 | 11:00 | `make scenario S=technical_webhook` | "No SSO/webhook content in the knowledge base at all — watch what it does with a question it can't answer." Point at `citations=0.00` and gate G3 red. **"It doesn't guess. An empty gate beats a confident wrong answer."** |
| 6 | 13:00 | `TASKFLOW_LLM_MODE=local_only` then re-run #1 (`make local`, or `--scenario` with the env var set) → Ollama drafts it | "Same pipeline, local model, no internet. My cloud API keys are temporary, so the system had to work without them." |
| 7 | 14:00 | Edit `config/thresholds.yaml`'s `refund` threshold down a few points, re-run `refund_prorated` → now auto-sends | "Config, not code. No restart." Revert the edit before continuing. |
| 8 | 14:30 | `docs/13_PRODUCTION_READINESS_GAP.md` | "What I'd build next, and roughly what each costs — Gmail push and confidence calibration are the two already underway." |

**Steps 3 and 5 are the two moments that decide whether you're taken seriously.**
Rehearse those specifically, out loud, until they're smooth.

## The scenarios and what each proves

| Scenario | Expected action | reason_code | Proves |
|---|---|---|---|
| `billing_payment_methods` | AUTO_SEND | `auto_send` | the full grounded RAG path, start to finish |
| `refund_750` | HUMAN_REVIEW | `G1_policy_critical` | governance is a veto, not a weight |
| `complaint_legal` | HUMAN_REVIEW | `G1_policy_critical` | a legal threat is caught in the customer's own words, before any model runs |
| `account_cancel` | HUMAN_REVIEW | `G1_policy_critical` | data-deletion requests route to policy, not to model judgment |
| `technical_webhook` | HUMAN_REVIEW | `G3_citations_resolve` | no matching knowledge → honest abstention, not a hallucinated answer |
| `pii_card` | HUMAN_REVIEW | `G3_citations_resolve` | an ambiguous dispute correctly asks for clarification instead of guessing |
| `refund_prorated` | HUMAN_REVIEW | `low_confidence` | the quality threshold is a real bar, not a rubber stamp — grounded and cited, still short |

`tests/e2e/test_scenarios.py` asserts routing decisions and reason codes with its
own fixtures, not this exact table — it never asserts generated prose, because model
output isn't deterministic and decisions are.

## Questions you will be asked, and the honest answers

- *"How do you know the classifier is any good?"* → run `make eval` and read
  `docs/metrics/classification.md` live; give both the keyword-fallback and LLM-path
  numbers and the gap between them, not a number memorized in advance.
- *"What happens if the LLM hallucinates a refund?"* → gate G1, deterministic, here's the test.
- *"What if someone puts instructions in an email?"* → routing is a classifier, not a
  model; policy is regex on the output; the model has no tools. Injection changes the
  prose, never the decision.
- *"Why no Docker / Kubernetes / Kafka?"* → sized to the problem; here's the migration path.
- *"Why not LangGraph?"* → ADR-005, with the threshold at which I'd add it back.
- *"What did you get wrong?"* → **the confidence formula.** Policy started as a 0.15
  weight; a violating draft scored 0.85 against an 0.80 threshold and would have sent.
  Lead with this if it doesn't come up.
- *"Does the fast path / spam filter you mentioned exist?"* → No — if asked, say so
  directly. Two enum values (`TEMPLATE_SENT`, `REJECTED_SPAM`) were reserved for
  them and nothing produces them yet; they're not part of this walkthrough.

## Insurance

- `TASKFLOW_LLM_MODE=local_only` (`make local`) — the entire demo works with no
  network.
- No automated reset script, seed script, or backup recording exists yet
  (`data/taskflow.db` isn't tracked in git, so there's no committed snapshot to
  revert to either). If a run leaves bad state: `make preflight` to diagnose,
  `rm -rf data/qdrant && make ingest` to rebuild vectors, or
  `rm data/taskflow.db && make migrate` for a genuinely empty database — the demo
  scenarios don't depend on any pre-seeded rows, they create what they need. Building
  `make demo-reset` and a backup recording script is worth doing before a live
  audience — see `docs/13_PRODUCTION_READINESS_GAP.md` if that isn't scoped yet.
- Phone hotspot ready; venue network tested in advance.
