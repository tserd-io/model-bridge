"""User-visible routing, usage and duplicate-request behavior."""

import pytest
from tests.support.harness import payload

pytestmark = pytest.mark.acceptance


# A complete request returns the selected model and provider-reported usage.
@pytest.mark.parametrize(
    "task,expected",
    [("simple", "fast"), ("complex", "balanced"), ("high_risk", "balanced")],
)
def test_routing_and_usage(app_factory, task, expected):
    gateway = app_factory()
    with gateway.client() as client:
        response = client.post(
            "/chat", json=payload(task_type=task, model_preference="balanced")
        )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == f"fake-{expected}"
    assert body["route"] == task
    assert body["input_tokens"] == 10
    assert body["output_tokens"] == 5
    assert body["estimated_cost_usd"] == 0.001
    assert body["human_review_required"] is (task == "high_risk")
    assert gateway.primary.calls[0]["model_preference"] == expected


# Replay avoids another provider call, while conflicting input cannot replace a result.
def test_replay_and_conflict(app_factory):
    gateway = app_factory()
    with gateway.client() as client:
        first = client.post("/chat", json=payload())
        replay = client.post("/chat", json=payload())
        conflict = client.post("/chat", json=payload(message="changed"))
    assert first.status_code == replay.status_code == 200
    assert replay.json() == {**first.json(), "cache_hit": True}
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert conflict.status_code == 409
    assert len(gateway.primary.calls) == 1


# Separate trusted tenants can independently use the same request ID.
def test_tenant_separation(app_factory):
    gateway = app_factory()
    with gateway.client() as client:
        first = client.post(
            "/chat", headers={"x-test-tenant": "a"}, json=payload(message="first")
        )
        second = client.post(
            "/chat", headers={"x-test-tenant": "b"}, json=payload(message="second")
        )
        replay = client.post(
            "/chat", headers={"x-test-tenant": "a"}, json=payload(message="first")
        )
    assert first.status_code == second.status_code == replay.status_code == 200
    assert second.json()["content"] == "Fake response: second"
    assert replay.json()["content"] == first.json()["content"]
    assert replay.json()["tenant_id"] == "a"
    assert len(gateway.primary.calls) == 2
