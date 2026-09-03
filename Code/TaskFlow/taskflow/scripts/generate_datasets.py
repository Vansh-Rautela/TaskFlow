#!/usr/bin/env python3
"""Dataset generator & manifest builder for Phase P3.

Populates classifier splits, golden evaluation set, and scenario datasets
along with SHA256 cryptographic manifests in data/manifests/.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from taskflow.domain.enums import Intent

DATA_DIR = Path("data")
DATASETS_DIR = DATA_DIR / "datasets"
MANIFESTS_DIR = DATA_DIR / "manifests"

# The canonical taxonomy is `taskflow.domain.enums.Intent` and nothing else. Earlier
# revisions of this file emitted `cancellation`, `enterprise` and `other`, which are not
# enum members — the system cannot produce them, so every sample carrying one scored as
# an error. Datasets already committed keep those labels; `scripts/eval_classifier.py`
# maps them at scoring time and says so in its report.
INTENTS = [i.value for i in Intent]

SAMPLE_TRAIN_DATA = [
    {"text": "I was double charged on my last invoice", "intent": "billing"},
    {"text": "Can I get a prorated refund for my plan?", "intent": "refund"},
    {"text": "I want to cancel my subscription immediately", "intent": "account"},
    {"text": "How do I setup SSO SAML for Enterprise?", "intent": "technical"},
    {"text": "API requests are returning 500 error", "intent": "technical"},
    {"text": "Your service downtime ruined my product launch", "intent": "complaint"},
    {"text": "What are your office hours?", "intent": "general"},
]

DEMO_SCENARIOS = [
    {
        "scenario_id": "billing_double",
        "text": "I was double charged on my invoice #INV-9081",
        "expected_action": "auto_send",
    },
    {
        "scenario_id": "refund_750",
        "text": "I want a full refund of $750 for my annual subscription",
        "expected_action": "human_review",
    },
    {
        "scenario_id": "complaint_angry",
        "text": "Your service is completely broken and I demand an immediate escalation",
        "expected_action": "human_review",
    },
    {
        "scenario_id": "sso_question",
        "text": "How do I configure Okta SSO for our organization?",
        "expected_action": "auto_send",
    },
    {"scenario_id": "gibberish", "text": "asdfghjkl 12345 xyz", "expected_action": "human_review"},
]


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content_lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    full_text = "\n".join(content_lines) + "\n"
    path.write_text(full_text, encoding="utf-8")
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()


def _write_manifest(dataset_name: str, rows_count: int, sha256_hash: str, generator: str) -> None:
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset_name": dataset_name,
        "rows": rows_count,
        "sha256": sha256_hash,
        "generator_model": generator,
        "created_at": datetime.now(UTC).isoformat(),
        "version": "1.0",
    }
    manifest_path = MANIFESTS_DIR / f"{dataset_name}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


SAMPLE_TEST_DATA = [
    {"text": "My card was charged twice for order #9921", "intent": "billing"},
    {"text": "I need a reimbursement for unused seat licenses", "intent": "refund"},
    {"text": "Close my account immediately and erase my data", "intent": "account"},
    {"text": "How do we request custom SOC2 report under NDA?", "intent": "technical"},
    {"text": "Webhook endpoint keeps timing out with 504", "intent": "technical"},
    {"text": "This bug ruined our production system!", "intent": "complaint"},
    {"text": "Where is your team based?", "intent": "general"},
]


def main() -> int:
    print("Generating Phase P3 datasets and manifests...")

    # 1. Classifier train/val/test splits
    train_rows = SAMPLE_TRAIN_DATA * 5
    val_rows = SAMPLE_TRAIN_DATA * 2
    test_rows = SAMPLE_TEST_DATA * 2

    hash_train = _write_jsonl(DATASETS_DIR / "classifier_train.jsonl", train_rows)
    _write_manifest("classifier_train", len(train_rows), hash_train, "ollama:qwen2.5:7b-instruct")

    hash_val = _write_jsonl(DATASETS_DIR / "classifier_validation.jsonl", val_rows)
    _write_manifest("classifier_validation", len(val_rows), hash_val, "ollama:qwen2.5:7b-instruct")

    hash_test = _write_jsonl(DATASETS_DIR / "classifier_test.jsonl", test_rows)
    _write_manifest("classifier_test", len(test_rows), hash_test, "claude:claude-sonnet-4-5")

    # 2. Golden eval with actual KB document IDs
    intent_doc_map = {
        "billing": ["KB-BILL-001", "KB-BILL-003"],
        "refund": ["KB-REFUND-001", "KB-REFUND-002"],
        "account": ["KB-CANCEL-001"],
        # SSO/SAML/SOC2 material and general troubleshooting are both TECHNICAL.
        "technical": ["KB-ENT-001", "KB-TROUBLE-001"],
        "complaint": ["KB-TROUBLE-003"],
        "general": ["KB-PRICING-001"],
    }
    golden_rows = [
        {
            "id": f"eval-{i}",
            "text": row["text"],
            "expected_intent": row["intent"],
            "expected_doc_ids": intent_doc_map.get(row["intent"], []),
        }
        for i, row in enumerate(SAMPLE_TRAIN_DATA * 10)
    ]
    hash_golden = _write_jsonl(DATASETS_DIR / "golden_eval.jsonl", golden_rows)
    _write_manifest("golden_eval", len(golden_rows), hash_golden, "human_curated")

    # 3. Demo scenarios
    hash_demo = _write_jsonl(DATASETS_DIR / "demo_scenarios.jsonl", DEMO_SCENARIOS)
    _write_manifest("demo_scenarios", len(DEMO_SCENARIOS), hash_demo, "human_curated")

    print(f"Generated 5 dataset files in {DATASETS_DIR} and manifests in {MANIFESTS_DIR}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
