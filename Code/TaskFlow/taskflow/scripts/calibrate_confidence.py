#!/usr/bin/env python3
"""Confidence weight calibration.

`config/thresholds.yaml`'s five quality-signal `weights` are heuristics today —
stated as such in the config comments, never presented as measured probabilities.
This script measures whether a different weighting would have separated correct
drafts from incorrect ones better, over a labeled sample, and reports the result.
It never touches the eight safety gates: every sample here is one where
`ConfidenceBreakdown.failed_gates` was already empty, exactly mirroring how
`services/confidence/scorer.py::decide()` only reaches the weighted score once
every gate has already passed. Safety is a veto here too — calibration cannot move
a gate into the weighted average, only re-weigh the quality signals gates don't
already cover.

Two label sources:

  --source golden (default)  Runs the real pipeline over data/datasets/golden_eval.jsonl
                              (label = did the draft cite a chunk from an expected
                              document?) and the gates-passed rows of
                              demo_scenarios.jsonl (label = expected_action).
                              Always available; the same synthetic-first convention
                              scripts/generate_datasets.py already uses for the
                              classifier.
  --source live               Joins real ReviewItems -> their trace's stored
                              ConfidenceBreakdown, using APPROVED/REJECTED and edit
                              size (EditRecord.char_diff) as the outcome label.
                              Honest about what it finds: today's database holds a
                              handful of reviews, nowhere near enough to trust a
                              reweighting, and the report says so plainly rather
                              than fabricating confidence from a small sample.

Fitting is a bounded grid search (step 0.1) over the 5 weights, kept non-negative
and summing to exactly 1.0 so `scripts/preflight.py`'s "weights sum to 1.0" check
keeps passing — five numbers don't need a gradient method, and this project has no
ML dependency to spare for one.

Never writes `config/thresholds.yaml` without --apply, and even then only when the
sample size clears MIN_SAMPLES_FOR_CONFIDENCE — safety-adjacent config gets a human
in the loop the same way a low-confidence draft does.

    uv run python scripts/calibrate_confidence.py
    uv run python scripts/calibrate_confidence.py --source live
    uv run python scripts/calibrate_confidence.py --apply
"""

import argparse
import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from taskflow.bootstrap import runtime
from taskflow.config.settings import settings, thresholds_config
from taskflow.domain.enums import Channel, ReviewState
from taskflow.domain.models import InboundMessage
from taskflow.pipeline.orchestrator import run_pipeline

DATASET_DIR = Path("data/datasets")
METRICS_OUT_PATH = Path("docs/metrics/calibration.md")
THRESHOLDS_PATH = Path("config/thresholds.yaml")

SIGNAL_NAMES = [
    "citation_coverage",
    "grounding_entailment",
    "retrieval_relevance",
    "intent_confidence",
    "tone_alignment",
]
GRID_STEP = 0.1
MIN_SAMPLES_FOR_CONFIDENCE = 30
# An edit under this fraction of the original draft's length counts as "mostly
# right" (label=1); at or above it counts as "mostly wrong" (label=0).
EDIT_RATIO_LABEL_CUTOFF = 0.15


@dataclass
class LabeledSample:
    signals: dict[str, float]
    threshold: float
    label: int  # 1 = should have auto-sent, 0 = should not have
    provenance: str


def _load_jsonl(filename: str) -> list[dict]:
    path = DATASET_DIR / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def _run_and_score(rt, text: str, provenance: str):
    """Run one message through the real pipeline. Returns the resulting trace, or
    None if a gate failed — those aren't calibration candidates."""
    msg_id = uuid.uuid4().hex[:8]
    msg = InboundMessage(
        message_id=msg_id,
        dedupe_key=InboundMessage.make_dedupe_key(Channel.CONSOLE, f"calib-{msg_id}"),
        tenant_id=settings().taskflow_tenant_id,
        channel=Channel.CONSOLE,
        sender="calibration@taskflow.local",
        subject=provenance,
        body_text=text,
        body_redacted=text,
        provider_message_id=f"calib-{msg_id}",
        received_at=datetime.now(UTC),
    )
    await run_pipeline(msg, rt.deps)
    trace = (await rt.trace_repo.recent(limit=1))[0]
    if trace.confidence is None or trace.confidence.failed_gates:
        return None
    return trace


def _signals_of(breakdown) -> dict[str, float]:
    return {name: getattr(breakdown, name) for name in SIGNAL_NAMES}


async def _collect_golden_samples(rt) -> list[LabeledSample]:
    rows = _load_jsonl("golden_eval.jsonl")
    seen_text: set[str] = set()
    samples = []
    for row in rows:
        text = row.get("text", "")
        if not text or text in seen_text:
            continue  # golden_eval repeats each of 7 questions ten times
        seen_text.add(text)

        trace = await _run_and_score(rt, text, f"golden:{row.get('id', text[:24])}")
        if trace is None:
            continue

        expected_docs = set(row.get("expected_doc_ids", []))
        cited_docs = {
            c.chunk_id.split(":")[0] for c in (trace.draft.citations if trace.draft else [])
        }
        label = 1 if cited_docs & expected_docs else 0
        samples.append(
            LabeledSample(
                signals=_signals_of(trace.confidence),
                threshold=trace.confidence.threshold,
                label=label,
                provenance=f"golden:{row.get('id', text[:24])}",
            )
        )
    return samples


async def _collect_demo_scenario_samples(rt) -> list[LabeledSample]:
    rows = _load_jsonl("demo_scenarios.jsonl")
    samples = []
    for row in rows:
        trace = await _run_and_score(rt, row["text"], f"demo:{row['scenario_id']}")
        if trace is None:
            continue  # this scenario demonstrates a gate veto, not a quality-score case
        label = 1 if row["expected_action"] == "auto_send" else 0
        samples.append(
            LabeledSample(
                signals=_signals_of(trace.confidence),
                threshold=trace.confidence.threshold,
                label=label,
                provenance=f"demo:{row['scenario_id']}",
            )
        )
    return samples


async def _collect_live_samples(rt) -> list[LabeledSample]:
    items = await rt.review_repo.all(limit=500)
    samples = []
    for item in items:
        if item.state in (ReviewState.PENDING, ReviewState.ESCALATED):
            continue  # no human outcome recorded yet

        trace = await rt.trace_repo.get(item.trace_id)
        if trace is None or trace.confidence is None or trace.confidence.failed_gates:
            continue  # gate-vetoed reviews never reached the weighted score

        if item.state == ReviewState.APPROVED:
            label = 1
        elif item.state == ReviewState.REJECTED:
            label = 0
        else:  # EDITED
            edits = await rt.review_repo.edits_for(item.review_id)
            if not edits:
                continue
            edit = edits[-1]
            edit_ratio = abs(edit.char_diff) / max(len(edit.original_draft), 1)
            label = 1 if edit_ratio < EDIT_RATIO_LABEL_CUTOFF else 0

        samples.append(
            LabeledSample(
                signals=_signals_of(trace.confidence),
                threshold=trace.confidence.threshold,
                label=label,
                provenance=f"live:{item.review_id}",
            )
        )
    return samples


def _weight_grid(step: float = GRID_STEP) -> list[dict[str, float]]:
    """Every combination of the 5 weights on `step`, non-negative, summing to 1.0."""
    n = round(1.0 / step)
    grid = []
    for a in range(n + 1):
        for b in range(n + 1 - a):
            for c in range(n + 1 - a - b):
                for d in range(n + 1 - a - b - c):
                    e = n - a - b - c - d
                    grid.append(
                        {
                            SIGNAL_NAMES[0]: round(a * step, 2),
                            SIGNAL_NAMES[1]: round(b * step, 2),
                            SIGNAL_NAMES[2]: round(c * step, 2),
                            SIGNAL_NAMES[3]: round(d * step, 2),
                            SIGNAL_NAMES[4]: round(e * step, 2),
                        }
                    )
    return grid


def _score(signals: dict[str, float], weights: dict[str, float]) -> float:
    return sum(signals[name] * weights[name] for name in SIGNAL_NAMES)


def _accuracy(samples: list[LabeledSample], weights: dict[str, float]) -> float:
    if not samples:
        return 0.0
    correct = sum(
        1 for s in samples if (1 if _score(s.signals, weights) >= s.threshold else 0) == s.label
    )
    return correct / len(samples)


def find_best_weights(
    samples: list[LabeledSample], current_weights: dict[str, float]
) -> tuple[dict[str, float], float, float]:
    """Returns (best_weights, best_accuracy, current_accuracy).

    Only ever replaces the current weights with a grid point that scores strictly
    higher accuracy on this sample — on a tie, or with no samples at all, keeps
    what's configured today rather than reshuffling safety-adjacent config for no
    measured gain.
    """
    current_acc = _accuracy(samples, current_weights)
    best_weights, best_acc = current_weights, current_acc
    for w in _weight_grid():
        acc = _accuracy(samples, w)
        if acc > best_acc + 1e-9:
            best_weights, best_acc = w, acc
    return best_weights, best_acc, current_acc


def _apply_weights(new_weights: dict[str, float]) -> None:
    """Rewrite only the 5 weight values in place, byte-for-byte otherwise — a full
    YAML round-trip would strip every comment in a file whose comments are half the
    point (see the file's own header: "sum to 1.0 — quality signals only")."""
    text = THRESHOLDS_PATH.read_text()
    for name, value in new_weights.items():
        pattern = re.compile(rf"^(\s*{re.escape(name)}:\s*)[0-9.]+(.*)$", re.MULTILINE)
        text, count = pattern.subn(rf"\g<1>{value:.2f}\g<2>", text, count=1)
        if count != 1:
            raise RuntimeError(
                f"expected exactly one '{name}:' line in {THRESHOLDS_PATH}, found {count}"
            )
    THRESHOLDS_PATH.write_text(text)


async def calibrate(source: str, apply: bool) -> int:
    current_weights = {k: float(v) for k, v in thresholds_config()["weights"].items()}

    async with runtime() as rt:
        if source == "golden":
            samples = await _collect_golden_samples(rt) + await _collect_demo_scenario_samples(rt)
        else:
            samples = await _collect_live_samples(rt)

    n = len(samples)
    positives = sum(s.label for s in samples)
    print(f"\n=== CONFIDENCE CALIBRATION (source={source}) ===")
    print(
        f"Usable (gates-passed) labeled samples: {n} ({positives} positive, {n - positives} negative)"
    )

    insufficient = n < MIN_SAMPLES_FOR_CONFIDENCE
    if insufficient:
        print(
            f"\nINSUFFICIENT DATA: need at least {MIN_SAMPLES_FOR_CONFIDENCE} samples to trust a "
            f"reweighting, have {n}. Reporting the mechanism only — treat anything below as "
            "directional, not a recommendation."
        )

    best_weights, best_acc, current_acc = (
        find_best_weights(samples, current_weights) if samples else (current_weights, 0.0, 0.0)
    )
    changed = any(abs(best_weights[k] - current_weights[k]) > 1e-9 for k in SIGNAL_NAMES)

    print(f"\nCurrent weights accuracy on this sample:   {current_acc:.1%}")
    print(f"Best grid-search weights accuracy:          {best_acc:.1%}")
    print("\n| Signal | Current | Suggested |")
    print("|---|---|---|")
    for name in SIGNAL_NAMES:
        print(f"| {name} | {current_weights[name]:.2f} | {best_weights[name]:.2f} |")

    weights_table = "\n".join(
        f"| {name} | {current_weights[name]:.2f} | {best_weights[name]:.2f} |"
        for name in SIGNAL_NAMES
    )
    samples_table = (
        "\n".join(
            f"| {s.provenance} | {s.label} | {_score(s.signals, current_weights):.3f} | {s.threshold:.2f} |"
            for s in samples
        )
        or "| *(none)* | | | |"
    )

    METRICS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = f"""# Confidence Weight Calibration

Generated by `scripts/calibrate_confidence.py --source {source}`. Every figure below
is measured against labeled samples, none is asserted. Gates are never touched —
only the 5 weighted quality signals feeding `services/confidence/scorer.py::compute()`,
and only over samples where every gate had already passed.

## Sample

| | |
|---|---|
| Source | `{source}` |
| Usable samples | {n} |
| Positive (should auto-send) | {positives} |
| Negative (should not) | {n - positives} |
| Minimum trusted sample size | {MIN_SAMPLES_FOR_CONFIDENCE} |

{
        "**INSUFFICIENT DATA.** The suggestion below is the mechanism working, not a "
        "recommendation — a sample this small can overfit five weights easily. Re-run "
        "once more labeled outcomes exist (`--source live` grows with every reviewed "
        "item; `--source golden` grows if `golden_eval.jsonl` grows)."
        if insufficient
        else "Sample size clears the trust threshold."
    }

## Result

| | Accuracy on this sample |
|---|---|
| Current weights | {current_acc:.1%} |
| Best grid-search weights | {best_acc:.1%} |

{
        "No change: nothing on the grid beat the current weights on this sample."
        if not changed
        else "A different weighting scored higher on this sample:"
    }

| Signal | Current | Suggested |
|---|---|---|
{weights_table}

## Samples scored

| Provenance | Label | Score (current weights) | Threshold |
|---|---|---|---|
{samples_table}

## Method

Bounded grid search, step {GRID_STEP}, over the 5 weights — non-negative, summing to
1.0 (so `scripts/preflight.py`'s "weights sum to 1.0" check keeps passing). Picks the
grid point with the highest accuracy predicting the label from `score >= threshold`;
ties keep the current weights rather than reshuffling config for no measured gain.
"""
    await asyncio.to_thread(METRICS_OUT_PATH.write_text, report, "utf-8")
    print(f"\nPublished report to {METRICS_OUT_PATH}")

    if apply:
        if insufficient:
            print("\nRefusing --apply: sample size is below the trust threshold.")
        elif not changed:
            print(
                "\n--apply: no change to write — the grid search agreed with the current weights."
            )
        else:
            _apply_weights(best_weights)
            print(f"\n--apply: wrote new weights to {THRESHOLDS_PATH}. Diff is in the table above.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", default="golden", choices=["golden", "live"])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the suggested weights to config/thresholds.yaml (refused below the trust threshold).",
    )
    args = parser.parse_args()
    return asyncio.run(calibrate(args.source, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
