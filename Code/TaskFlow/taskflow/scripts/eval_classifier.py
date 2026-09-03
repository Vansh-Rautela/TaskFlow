#!/usr/bin/env python3
"""Intent classification evaluation.

Two paths exist and they are measured separately, because they perform very
differently and averaging them would describe neither:

  --path keyword   the deterministic fallback, offline, free
  --path llm       the real provider chain (costs money, needs a key)
  --path both      default

The previous version of this script fed `_fallback_classify` back through a fake
provider and reported the result as LLM accuracy. That measured the keyword
classifier against itself and labelled the number as something else. It also
unpacked two values from `classify_intent`, which returns three, so it raised
before printing anything.

    uv run python scripts/eval_classifier.py --path keyword
"""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path

from taskflow.adapters.llm.router import ProviderRouter
from taskflow.config.settings import provider_priority
from taskflow.services.classify.service import classify_intent

TEST_DATASET_PATH = Path("data/datasets/classifier_test.jsonl")
METRICS_OUT_PATH = Path("docs/metrics/classification.md")

# The datasets were generated against a 7-label taxonomy that predates the `Intent`
# enum: `cancellation`, `enterprise` and `other` are not members of it and the system
# cannot, by construction, emit them. Scoring against them would count correct
# classifications as errors.
#
# The mapping below follows the taxonomy definitions in `services/classify`:
# closing an account is ACCOUNT, SSO/SAML/SOC2 is TECHNICAL, and an unclassifiable
# message is UNKNOWN. It is applied to the *labels*, reported explicitly, and never
# to the predictions.
LEGACY_LABEL_MAP = {
    "cancellation": "account",
    "enterprise": "technical",
    "other": "unknown",
}


def _load_rows() -> list[dict]:
    if not TEST_DATASET_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in TEST_DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class Result:
    def __init__(self, label: str) -> None:
        self.label = label
        self.total = 0
        self.correct = 0
        self.latency_ms = 0.0
        self.tp: dict[str, int] = defaultdict(int)
        self.fp: dict[str, int] = defaultdict(int)
        self.fn: dict[str, int] = defaultdict(int)
        self.confusions: dict[tuple[str, str], int] = defaultdict(int)
        self.remapped = 0
        self.error: str | None = None

    @property
    def accuracy(self) -> float:
        return (self.correct / self.total * 100.0) if self.total else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return (self.latency_ms / self.total) if self.total else 0.0

    def macro_f1(self) -> float:
        labels = set(self.tp) | set(self.fp) | set(self.fn)
        scores = []
        for label in labels:
            tp, fp, fn = self.tp[label], self.fp[label], self.fn[label]
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            recall = tp / (tp + fn) if (tp + fn) else 0.0
            scores.append(
                2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            )
        return sum(scores) / len(scores) if scores else 0.0

    def record(self, expected: str, predicted: str, elapsed_ms: float) -> None:
        self.total += 1
        self.latency_ms += elapsed_ms
        if predicted == expected:
            self.correct += 1
            self.tp[expected] += 1
        else:
            self.fp[predicted] += 1
            self.fn[expected] += 1
            self.confusions[(expected, predicted)] += 1


async def run_path(label: str, rows: list[dict], router: ProviderRouter | None) -> Result:
    result = Result(label)
    for row in rows:
        raw = str(row.get("intent", "")).lower()
        expected = LEGACY_LABEL_MAP.get(raw, raw)
        if raw != expected:
            result.remapped += 1
        start = time.perf_counter_ns()
        try:
            intent, _confidence, _call = await classify_intent(row.get("text", ""), router=router)
        except Exception as err:  # a broken provider must not be reported as 0% accuracy
            result.error = f"{type(err).__name__}: {err}"
            return result
        elapsed = (time.perf_counter_ns() - start) / 1_000_000
        result.record(expected, intent.value.lower(), elapsed)
    return result


def render(result: Result) -> str:
    if result.error:
        return f"\n=== {result.label.upper()} ===\n  did not run: {result.error}\n"

    lines = [
        f"\n=== {result.label.upper()} ===",
        f"  Samples:      {result.total}",
        f"  Accuracy:     {result.accuracy:.2f}%",
        f"  Macro F1:     {result.macro_f1():.4f}",
        f"  Avg latency:  {result.avg_latency_ms:.1f} ms",
    ]
    if result.remapped:
        lines.append(
            f"  Labels remapped from the legacy taxonomy: {result.remapped}/{result.total}"
        )
    if result.confusions:
        lines.append("  Most common confusions (expected -> predicted):")
        for (exp, pred), n in sorted(result.confusions.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"    {exp} -> {pred}: {n}")
    return "\n".join(lines)


async def evaluate(path: str) -> int:
    rows = _load_rows()
    if not rows:
        print(f"Error: {TEST_DATASET_PATH} not found or empty. Run scripts/generate_datasets.py.")
        return 1

    legacy = sum(1 for r in rows if str(r.get("intent", "")).lower() in LEGACY_LABEL_MAP)
    print(f"Loaded {len(rows)} labelled samples from {TEST_DATASET_PATH}.")
    if legacy:
        print(
            f"  {legacy} carry legacy labels not in the Intent enum; mapped as "
            + ", ".join(f"{k}->{v}" for k, v in LEGACY_LABEL_MAP.items())
        )
    results: list[Result] = []

    if path in ("keyword", "both"):
        # router=None takes the deterministic keyword path.
        results.append(await run_path("keyword classifier (offline fallback)", rows, None))

    if path in ("llm", "both"):
        print(f"Provider priority: {provider_priority()} — this path makes real API calls.")
        results.append(
            await run_path("llm classifier (real provider chain)", rows, ProviderRouter())
        )

    for result in results:
        print(render(result))

    METRICS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = ["# Classification Metrics", "", "Generated by `scripts/eval_classifier.py`.", ""]
    body.append("| Path | Samples | Accuracy | Macro F1 | Avg latency |")
    body.append("|---|---|---|---|---|")
    for r in results:
        if r.error:
            body.append(f"| {r.label} | — | did not run | — | {r.error} |")
        else:
            body.append(
                f"| {r.label} | {r.total} | {r.accuracy:.2f}% | {r.macro_f1():.4f} | "
                f"{r.avg_latency_ms:.1f} ms |"
            )
    if any(r.remapped for r in results):
        body += [
            "",
            "## Label mapping",
            "",
            "The datasets were generated against a 7-label taxonomy that predates the",
            "`Intent` enum. Three of its labels are not enum members, so the system cannot",
            "emit them and scoring against them would count correct answers as errors.",
            "Labels — never predictions — are mapped as follows before scoring:",
            "",
            "| Dataset label | Scored as | Why |",
            "|---|---|---|",
            "| `cancellation` | `account` | Closing an account and cancelling a subscription are ACCOUNT in the taxonomy |",
            "| `enterprise` | `technical` | SSO, SAML and SOC2 are TECHNICAL |",
            "| `other` | `unknown` | The abstain target |",
        ]
    body += [
        "",
        "The two paths are reported separately on purpose. The keyword classifier is the",
        "offline fallback used when every provider fails; it is a substring matcher and is",
        "expected to score well below the LLM path. Averaging them would describe neither.",
        "",
        f"Test set: `{TEST_DATASET_PATH}` (held out; `scripts/check_leakage.py` asserts no",
        "overlap with the training split).",
    ]
    await asyncio.to_thread(METRICS_OUT_PATH.write_text, "\n".join(body) + "\n", "utf-8")
    print(f"\nPublished report to {METRICS_OUT_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", choices=["keyword", "llm", "both"], default="both")
    return asyncio.run(evaluate(parser.parse_args().path))


if __name__ == "__main__":
    raise SystemExit(main())
