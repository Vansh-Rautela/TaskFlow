# 07 — Evaluation Methodology

Every number that appears on a slide must be reproducible by one command in this file.

## Classifier

```bash
make train        # trains, then writes docs/metrics/classifier.md
```

**Splits.** Train and validation from generator A (Ollama, prompt A, personas A).
Test from generator B (Claude, prompt B, personas B). Golden set hand-written.
`scripts/check_leakage.py` must exit 0 first.

**Metrics reported on both the synthetic test set and the golden set:** accuracy,
macro-F1, per-class precision, per-class recall, confusion matrix, abstain rate.

**Expected shape of the result:**

```
                     acc    macroF1   abstain   complaint-recall
synthetic held-out   0.94     0.93      0.04          0.97
hand-written golden  0.81     0.78      0.15          0.91
gap                 -0.13    -0.15     +0.11         -0.06
```

**The gap is the finding, not the embarrassment.** How to narrate it:

> "The thirteen-point gap is what synthetic evaluation costs you. Train and test come from
> different model families with different prompts, and the golden set is hand-written, so
> 0.81 is the number I'd defend. Abstention rises to 15% on unfamiliar phrasing — those
> become human escalations rather than wrong routes, which is the failure mode we want."

Anyone who hears that does not think you scored 0.81. They think you know how to evaluate.

**Abstain threshold.** Chosen from the validation precision/coverage curve targeting ~0.95
precision; default 0.55, stored in `config/thresholds.yaml`. Below it, intent becomes
`unknown` and gate G5 fails.

**Complaint recall is safety-critical** — a missed complaint means an automated reply to
an angry customer. Hard floor 0.85, enforced by a regression test.

**Do not claim calibration.** Logistic regression outputs are not calibrated probabilities
unless you calibrate them. If you have time, add temperature scaling on the validation
split plus a reliability diagram, and be explicit that only the *classifier* is calibrated
— the composite confidence score is a heuristic and always will be.

## Retrieval — measured independently of any LLM

```bash
uv run python scripts/eval_retrieval.py     # writes docs/metrics/retrieval.md
```

Against `golden_eval.jsonl` rows carrying `expected_doc_ids`:

| Metric | Meaning |
|---|---|
| recall@5 | fraction of queries whose gold doc is in the reranked top 5 |
| MRR@10 | mean reciprocal rank of the first gold doc |
| gap-rate | fraction flagged insufficient — split into true gaps vs false negatives |
| hybrid vs dense-only | recall@5 with and without the sparse branch |

That last row is what justifies the BM25 branch with a number instead of an assertion.
Expect the delta to come mostly from exact-term queries (error codes, plan names, amounts).

## Generation — measured with retrieval held fixed

Give the drafting model **gold context** so retrieval quality cannot mask drafting
quality. Conflating the two is the most common RAG evaluation mistake, and keeping them
separate lets you say which component caused a regression.

| Metric | How |
|---|---|
| schema validity | % first-attempt parses (should be 100% with constrained decoding) |
| citation validity | % cited chunk ids that exist in the provided context |
| unsupported-claim rate | grounding validator verdicts over 20 drafts, spot-checked by hand |
| policy violation rate | policy engine hits per 100 drafts |
| auto-send rate | % of golden-eval messages that clear all gates and the threshold |

Run this for **both** providers and publish both columns. The local model will be worse;
showing the difference is the honest version of "we have a fallback".

**Target auto-send rate: 40–60%.** Below 40% the system is theatre; above 60% on synthetic
data you have probably set thresholds too low.

## Regression guard

`src/taskflow/ml/artifacts/expected_metrics.json` holds the current golden-set numbers.
A test fails if macro-F1 drops more than 0.03 or complaint recall falls below 0.85. Update
that file only in the same commit as the change that improved the metrics.
