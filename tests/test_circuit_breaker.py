import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from fastapi.testclient import TestClient

import model_bridge.main as main_module
from model_bridge.config.models import Settings
from model_bridge.execution.circuit_breaker import CircuitBreaker
from model_bridge.execution.circuit_breaker import CircuitOpenError
from model_bridge.execution.generation import CircuitUnavailableError
from model_bridge.execution.rate_limit import SlidingWindowRateLimiter
from model_bridge.providers.contracts import GenerationResult


# Supplies a breaker and a controllable clock without real waiting.
@pytest.fixture
def breaker_clock():
    now = [100.0]
    breaker = CircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=30,
        clock=lambda: now[0],
    )
    return breaker, now


# Checks that success resets consecutive failures before opening occurs.
def test_success_resets_failure_count(breaker_clock):
    breaker, _ = breaker_clock

    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "success")
    breaker.finish(breaker.acquire(), "failure")
    assert breaker.state == "closed"

    transition = breaker.finish(breaker.acquire(), "failure")
    assert transition == ("closed", "open")

    with pytest.raises(CircuitOpenError) as error:
        breaker.acquire()

    assert error.value.retry_after == 30


# Checks that only one competing caller receives a recovery permit.
def test_half_open_allows_one_probe(breaker_clock):
    breaker, now = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "failure")
    now[0] += 30

    # Attempts admission and records rejections without provider work.
    def acquire_or_none(_):
        try:
            return breaker.acquire()
        except CircuitOpenError:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(acquire_or_none, range(12)))

    permits = [permit for permit in results if permit is not None]
    assert len(permits) == 1
    assert breaker.state == "half_open"

    assert breaker.finish(permits[0], "success") == (
        "half_open",
        "closed",
    )


# Checks that failed or inconclusive probes reopen and restart cooldown.
@pytest.mark.parametrize("outcome", ["failure", "ignored"])
def test_unsuccessful_probe_reopens(breaker_clock, outcome):
    breaker, now = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "failure")
    now[0] += 30

    probe = breaker.acquire()
    assert breaker.finish(probe, outcome) == ("half_open", "open")

    with pytest.raises(CircuitOpenError) as error:
        breaker.acquire()

    assert error.value.retry_after == 30


# Checks that a late success cannot erase a newer outage.
def test_old_success_cannot_close_open_circuit(breaker_clock):
    breaker, _ = breaker_clock
    old_call = breaker.acquire()

    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "failure")

    assert breaker.finish(old_call, "success") is None
    assert breaker.state == "open"


# Checks that a call's failure cannot be counted twice.
def test_permit_can_only_be_finished_once(breaker_clock):
    breaker, _ = breaker_clock
    permit = breaker.acquire()
    breaker.finish(permit, "failure")

    with pytest.raises(ValueError):
        breaker.finish(permit, "failure")


# Reads the saved request without changing its state and always closes SQLite.
def stored_request(service, request_id):
    with closing(sqlite3.connect(service.request_store.database_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT * FROM requests
            WHERE tenant_id = ? AND request_id = ?
            """,
            ("test-tenant", request_id),
        ).fetchone()

    assert row is not None
    return dict(row)


# Demonstrates that circuit rejection preserves a retryable record, rejects
# changed input, and allows the original request to recover without duplication.
def test_api_circuit_rejection_preserves_retryable_storage(
    isolated_chat_service,
    breaker_clock,
):
    service = isolated_chat_service
    service.chat_rate_limiter = SlidingWindowRateLimiter(100, 60)
    breaker, now = breaker_clock

    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "failure")
    service.primary_breaker = breaker

    payload = {
        "request_id": "circuit-recovery",
        "message": "hello",
        "model_preference": "fast",
        "max_tokens": 10,
    }

    # Checks that recovery reclaimed and cleared the rejected record
    # before provider work begins.
    async def recover(**kwargs):
        claimed = stored_request(service, payload["request_id"])
        assert claimed["status"] == "in_progress"
        assert claimed["detail"] is None
        assert claimed["response_json"] is None
        assert claimed["attempts"] == 0
        assert claimed["request_hash"] == initial["request_hash"]
        assert claimed["created_at"] == initial["created_at"]

        return GenerationResult(content="recovered", model="fake-fast")

    complete = AsyncMock(side_effect=recover)
    service.primary_provider = SimpleNamespace(complete=complete)

    with TestClient(main_module.app) as client:
        rejected = client.post("/chat", json=payload)

        assert rejected.status_code == 503
        assert rejected.headers["Retry-After"] == "30"
        assert rejected.json()["attempts"] == 0
        complete.assert_not_awaited()

        initial = stored_request(service, payload["request_id"])
        assert initial["status"] == "retryable"
        assert initial["attempts"] == 0
        assert initial["response_json"] is None
        assert initial["detail"] == rejected.json()["detail"]

        # Retrying too early must leave the request retryable.
        repeated = client.post("/chat", json=payload)
        assert repeated.status_code == 503
        assert repeated.headers["Retry-After"] == "30"

        still_retryable = stored_request(service, payload["request_id"])
        assert still_retryable["status"] == "retryable"
        assert still_retryable["attempts"] == 0
        assert still_retryable["request_hash"] == initial["request_hash"]
        complete.assert_not_awaited()

        # Retryability must not allow the ID to represent different input.
        conflict = client.post("/chat", json={**payload, "message": "different"})
        assert conflict.status_code == 409
        assert stored_request(service, payload["request_id"])["status"] == "retryable"
        complete.assert_not_awaited()

        now[0] += 30
        recovered = client.post("/chat", json=payload)

        assert recovered.status_code == 200
        assert recovered.json()["content"] == "recovered"

        saved = stored_request(service, payload["request_id"])
        assert saved["status"] == "success"
        assert saved["attempts"] == 1
        assert saved["detail"] is None
        assert saved["request_hash"] == initial["request_hash"]
        assert saved["created_at"] == initial["created_at"]
        assert json.loads(saved["response_json"])["content"] == "recovered"

        replay = client.post("/chat", json=payload)

    assert replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json()["cache_hit"] is True
    complete.assert_awaited_once()


# Demonstrates that an uncertain circuit error stays unknown in storage
# and repeating the request does not cause another generation attempt.
def test_api_circuit_error_preserves_previous_uncertainty(isolated_chat_service):
    service = isolated_chat_service
    service.chat_rate_limiter = SlidingWindowRateLimiter(100, 60)
    service.generate = AsyncMock(
        side_effect=CircuitUnavailableError(
            attempts=1,
            outcome_unknown=True,
            retry_after=30,
            provider="fake",
            model="fake-fast",
        )
    )

    payload = {
        "request_id": "uncertain-circuit",
        "message": "hello",
        "model_preference": "fast",
        "max_tokens": 10,
    }

    with TestClient(main_module.app) as client:
        response = client.post("/chat", json=payload)
        saved = stored_request(service, payload["request_id"])
        assert saved["status"] == "unknown"
        assert saved["attempts"] == 1
        assert saved["response_json"] is None

        repeated = client.post("/chat", json=payload)

    for result in (response, repeated):
        assert result.status_code == 202
        assert result.json()["status"] == "unknown"
        assert result.json()["attempts"] == 1
        assert "Retry-After" not in result.headers

    assert stored_request(service, payload["request_id"]) == saved
    service.generate.assert_awaited_once()


# Demonstrates that each configured provider supplies its own failure
# threshold and cooldown; matching primary/fallback names share one breaker.
@pytest.mark.parametrize("same_provider", [False, True])
def test_service_constructs_breakers_from_provider_settings(
    isolated_chat_service,
    monkeypatch,
    tmp_path,
    same_provider,
):
    data = isolated_chat_service.settings.model_dump()
    data["providers"]["fake"] = {
        "type": "fake",
        "circuit_breaker_failure_threshold": 2,
        "circuit_breaker_cooldown_seconds": 7,
    }
    data["providers"]["backup"] = {
        "type": "fake",
        "circuit_breaker_failure_threshold": 4,
        "circuit_breaker_cooldown_seconds": 19,
    }
    data["primary_provider"] = "fake"
    data["fallback_provider"] = "fake" if same_provider else "backup"
    data["storage"]["path"] = tmp_path / "construction.sqlite3"

    # Record construction while still returning real breaker instances.
    factory = Mock(side_effect=CircuitBreaker)
    monkeypatch.setattr(main_module, "CircuitBreaker", factory)

    service = main_module.create_chat_service(Settings.model_validate(data))
    expected_calls = [call(failure_threshold=2, cooldown_seconds=7)]

    if same_provider:
        assert service.primary_breaker is service.backup_breaker
    else:
        expected_calls.append(call(failure_threshold=4, cooldown_seconds=19))
        assert service.primary_breaker is not service.backup_breaker

    assert factory.call_count == len(expected_calls)
    factory.assert_has_calls(expected_calls, any_order=True)
