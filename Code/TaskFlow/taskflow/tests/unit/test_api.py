"""API tests.

The API is transport: authenticate, validate, delegate, serialise. These tests cover
that contract, not the pipeline behind it.
"""

import pytest
from fastapi.testclient import TestClient

from taskflow.api.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client against a real migrated database, with the LLM and vector store faked."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/api.db")
    from taskflow.config import settings as settings_module

    settings_module.settings.cache_clear()

    from taskflow.domain.enums import Intent
    from taskflow.domain.models import Citation, ClassificationOutput, DraftOutput
    from tests.fixtures.fakes import FakeLLMRouter, FakeReranker, FakeVectorStore, chunk

    text = "Refunds are prorated for unused seat licenses on annual plans."
    fake_router = FakeLLMRouter(
        responses={
            "classification": ClassificationOutput(
                intent=Intent.BILLING, confidence=0.95, reasoning="billing"
            ),
            "draft": DraftOutput(
                response_text=f"I am sorry for the confusion. {text} [kb:0]",
                citations=[Citation(chunk_id="kb:0", doc_title="Refunds")],
                tone="apologetic",
                complexity="simple",
                draft_confidence=0.9,
            ),
        }
    )
    fake_store = FakeVectorStore(
        chunks=[chunk("kb:0", text), chunk("kb:1", "Requests must be filed within 30 days.")],
        scores=[0.85, 0.6],
    )

    # The API builds itself through the composition root, so that is where the
    # adapters are swapped — one seam instead of one per entrypoint.
    fake_reranker = FakeReranker({"kb:0": 0.85, "kb:1": 0.60})
    monkeypatch.setattr("taskflow.bootstrap.ProviderRouter", lambda *a, **k: fake_router)
    monkeypatch.setattr("taskflow.bootstrap.QdrantVectorStore", lambda *a, **k: fake_store)
    monkeypatch.setattr("taskflow.bootstrap.CrossEncoderReranker", lambda *a, **k: fake_reranker)

    with TestClient(app) as c:
        c.fake_router = fake_router
        yield c

    settings_module.settings.cache_clear()


def _message(message_id: str = "api-1") -> dict:
    return {
        "message_id": message_id,
        "tenant_id": "taskflow-demo",
        "channel": "email",
        "sender": "customer@example.com",
        "subject": "Billing question",
        "body_text": "I was double charged on my invoice this month.",
    }


# ---------- open endpoints ----------


def test_root_is_open(client):
    res = client.get("/")
    assert res.status_code == 200
    assert res.json()["service"].startswith("TaskFlow")


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "taskflow-api"}


# ---------- authentication ----------


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/messages"),
        ("get", "/traces"),
        ("get", "/reviews"),
        ("get", "/metrics"),
        ("post", "/reviews/rev-1/approve"),
        ("post", "/reviews/rev-1/reject"),
    ],
)
def test_protected_endpoints_reject_anonymous_requests(client, method, path):
    """TASKFLOW_API_TOKEN was a configured setting that no endpoint enforced."""
    kwargs = {"json": {}} if method == "post" else {}
    res = getattr(client, method)(path, **kwargs)
    assert res.status_code == 401, f"{method.upper()} {path} served an unauthenticated request"


def test_wrong_token_is_rejected(client):
    res = client.get("/traces", headers={"Authorization": "Bearer not-the-token"})
    assert res.status_code == 401


# ---------- ingestion ----------


def test_ingest_returns_the_decision(client, auth_headers):
    res = client.post("/messages", json=_message(), headers=auth_headers)
    assert res.status_code == 200

    body = res.json()
    assert body["action"] in {"auto_send", "human_review"}
    assert body["reason_code"]
    assert body["threshold"] == pytest.approx(0.80), "billing must be judged at its own threshold"


def test_ingest_rejects_an_empty_body(client, auth_headers):
    payload = _message() | {"body_text": ""}
    assert client.post("/messages", json=payload, headers=auth_headers).status_code == 422


def test_duplicate_ingest_is_dropped(client, auth_headers):
    first = client.post("/messages", json=_message("dup-x"), headers=auth_headers).json()
    second = client.post("/messages", json=_message("dup-x"), headers=auth_headers).json()

    assert first["action"] != "dropped_duplicate"
    assert second["action"] == "dropped_duplicate"


def test_client_cannot_supply_body_redacted(client, auth_headers):
    """Redaction is the pipeline's job; a client must not be able to pre-empt it."""
    payload = _message("no-inject") | {"body_redacted": "clean"}
    res = client.post("/messages", json=payload, headers=auth_headers)
    assert res.status_code == 200

    trace = client.get("/traces", headers=auth_headers).json()[0]
    assert trace["intent"] is not None


# ---------- traces and metrics ----------


def test_trace_records_the_decision(client, auth_headers):
    client.post("/messages", json=_message("trace-1"), headers=auth_headers)

    traces = client.get("/traces", headers=auth_headers).json()
    assert len(traces) == 1

    detail = client.get(f"/traces/{traces[0]['trace_id']}", headers=auth_headers).json()
    assert detail["decision"]["reason_code"]
    assert detail["confidence"]["threshold"] == pytest.approx(0.80)
    assert detail["draft"]["response_text"]


def test_unknown_trace_is_404(client, auth_headers):
    assert client.get("/traces/nope", headers=auth_headers).status_code == 404


def test_metrics_are_derived_from_traces(client, auth_headers):
    for i in range(3):
        client.post("/messages", json=_message(f"metric-{i}"), headers=auth_headers)

    m = client.get("/metrics", headers=auth_headers).json()
    assert m["total_traces"] == 3
    assert sum(m["actions_breakdown"].values()) == m["decided_traces"]
    assert m["auto_send_rate_pct"] + m["human_review_rate_pct"] == pytest.approx(100.0)
    assert m["intents_breakdown"] == {"billing": 3}


def test_actioning_a_missing_review_is_409(client, auth_headers):
    res = client.post("/reviews/rev-missing/approve", json={}, headers=auth_headers)
    assert res.status_code == 409
