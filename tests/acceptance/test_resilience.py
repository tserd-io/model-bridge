"""Real retries, fallback and circuit handling through the API."""

import pytest
from model_bridge.execution.circuit_breaker import CircuitBreaker
from model_bridge.providers.contracts import RetryableProviderError
from tests.support.harness import payload
from tests.support.providers import ScriptedProvider

pytestmark = pytest.mark.acceptance


# A transient rejection recovers through the real retry loop, using the same request ID.
def test_retry_recovers(app_factory):
    provider = ScriptedProvider(RetryableProviderError("rate limited", status_code=429))
    gateway = app_factory(primary=provider)
    with gateway.client() as client:
        response = client.post("/chat", json=payload())
    assert response.status_code == 200
    assert response.json()["attempts"] == 2
    assert [(call["request_id"], call["attempt"]) for call in provider.calls] == [
        ("request-1", 1),
        ("request-1", 2),
    ]


# Independent fallback uses the remaining attempt budget and reports the actual provider.
def test_fallback_recovers(app_factory):
    primary = ScriptedProvider(RetryableProviderError("unavailable", status_code=503))
    fallback = ScriptedProvider(name="backup")
    gateway = app_factory(primary=primary, fallback=fallback)
    with gateway.client() as client:
        response = client.post("/chat", json=payload())
    assert response.status_code == 200
    assert response.json()["provider"] == "backup"
    assert response.json()["attempts"] == 2
    assert len(primary.calls) == len(fallback.calls) == 1


# The configured attempt cap prevents an unlimited retry loop.
def test_retry_budget_is_bounded(app_factory):
    provider = ScriptedProvider(
        *[RetryableProviderError("limited", status_code=429) for _ in range(3)]
    )
    gateway = app_factory(primary=provider)
    with gateway.client() as client:
        response = client.post("/chat", json=payload())
    assert response.status_code == 503
    assert response.json()["attempts"] == 3
    assert len(provider.calls) == 3


# An uncertain result is replayed without starting another remote operation.
def test_unknown_outcome_is_not_regenerated(app_factory):
    provider = ScriptedProvider(
        RetryableProviderError(
            "lost response", outcome_unknown=True, failure_type="timeout"
        )
    )
    gateway = app_factory(primary=provider, provider_options={"max_attempts": 1})
    with gateway.client() as client:
        first = client.post("/chat", json=payload())
        replay = client.post("/chat", json=payload())
    assert first.status_code == replay.status_code == 202
    assert first.json()["status"] == replay.json()["status"] == "unknown"
    assert len(provider.calls) == 1


# A provider failure opens the circuit; zero-call rejection remains retryable after cooldown.
def test_circuit_rejection_then_recovery(app_factory):
    provider = ScriptedProvider(RetryableProviderError("outage", status_code=503))
    gateway = app_factory(primary=provider, provider_options={"max_attempts": 1})
    clock = [0.0]
    gateway.service.primary_breaker = CircuitBreaker(1, 10, clock=lambda: clock[0])
    with gateway.client() as client:
        failed = client.post("/chat", json=payload("trigger"))
        rejected = client.post("/chat", json=payload("recover"))
        assert len(provider.calls) == 1
        clock[0] = 11
        recovered = client.post("/chat", json=payload("recover"))
    assert failed.status_code == rejected.status_code == 503
    assert rejected.json()["attempts"] == 0
    assert int(rejected.headers["Retry-After"]) > 0
    assert recovered.status_code == 200
    assert len(provider.calls) == 2
