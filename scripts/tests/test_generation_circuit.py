import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from scripts import generation
from scripts.circuit_breaker import CircuitBreaker
from scripts.contracts import (
    GenerationResult,
    PermanentProviderError,
    ProviderOutcomeUnknown,
    RetryableProviderError,
)


# Creates a controllable breaker that opens after one counted failure.
@pytest.fixture
def breaker_clock():
    now = [0.0]
    breaker = CircuitBreaker(1, 30, clock=lambda: now[0])
    return breaker, now


# Invokes generation with deterministic retry delays and explicit fake dependencies.
async def generate(provider, breaker, **kwargs):
    return await generation.llm_call(
        "circuit-test", "private prompt", "fast", 10,
        provider=provider,
        provider_name="fake",
        circuit_breaker=breaker,
        retry_base_delay_seconds=0,
        **kwargs,
    )


# Rejects an open circuit without provider calls, attempt metrics, or prompt logging.
def test_open_circuit_does_not_count_a_provider_attempt(breaker_clock, monkeypatch):
    breaker, _ = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    provider = SimpleNamespace(complete=AsyncMock())
    record = Mock()
    log = Mock()
    monkeypatch.setattr(generation, "record_provider_attempt", record)
    monkeypatch.setattr(generation, "log_event", log)

    with pytest.raises(generation.CircuitUnavailableError) as error:
        asyncio.run(generate(provider, breaker))

    assert error.value.attempts == 0
    assert error.value.retry_after == 30
    assert error.value.outcome_unknown is False
    provider.complete.assert_not_awaited()
    record.assert_not_called()
    assert log.call_args.args == ("circuit_call_rejected",)
    assert "private prompt" not in repr(log.call_args_list)


# Keeps the first actual fallback call numbered one when primary admission is rejected.
def test_open_primary_can_use_independent_fallback(breaker_clock):
    breaker, _ = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    primary = SimpleNamespace(complete=AsyncMock())
    fallback = SimpleNamespace(complete=AsyncMock(
        return_value=GenerationResult("ok", "fallback-model")
    ))

    result = asyncio.run(generate(
        primary, breaker,
        max_attempts=1,
        fallback_provider=fallback,
        fallback_provider_name="other",
        fallback_circuit_breaker=CircuitBreaker(),
    ))

    primary.complete.assert_not_awaited()
    assert fallback.complete.await_args.kwargs["attempt"] == 1
    assert result.attempts == 1
    assert result.provider == "other"


# Prevents a second adapter for the same backend from bypassing the open circuit.
def test_same_backend_fallback_shares_breaker(breaker_clock):
    breaker, _ = breaker_clock
    primary = SimpleNamespace(complete=AsyncMock(side_effect=RetryableProviderError(
        "unavailable", status_code=503, failure_type="http_503"
    )))
    fallback = SimpleNamespace(complete=AsyncMock())

    with pytest.raises(generation.CircuitUnavailableError) as error:
        asyncio.run(generate(
            primary, breaker,
            fallback_provider=fallback,
            fallback_provider_name="fake",
        ))

    assert error.value.attempts == 1
    assert primary.complete.await_count == 1
    fallback.complete.assert_not_awaited()


# Rejects contradictory breaker wiring instead of allowing same-backend bypass.
def test_same_backend_cannot_have_separate_breakers(breaker_clock):
    breaker, _ = breaker_clock
    provider = SimpleNamespace(complete=AsyncMock())
    with pytest.raises(ValueError, match="same backend"):
        asyncio.run(generate(
            provider, breaker,
            fallback_provider=SimpleNamespace(complete=AsyncMock()),
            fallback_provider_name="fake",
            fallback_circuit_breaker=CircuitBreaker(),
        ))
    provider.complete.assert_not_awaited()


# Preserves a prior timeout's uncertainty when the circuit rejects the next attempt.
def test_timeout_uncertainty_survives_circuit_rejection(breaker_clock):
    breaker, _ = breaker_clock
    provider = SimpleNamespace(complete=AsyncMock(side_effect=asyncio.TimeoutError()))
    with pytest.raises(generation.CircuitUnavailableError) as error:
        asyncio.run(generate(provider, breaker))
    assert error.value.attempts == 1
    assert error.value.outcome_unknown is True
    assert error.value.failure_type == "circuit_open"
    assert provider.complete.await_count == 1


# Keeps rate-limit retries separate from the provider-outage breaker.
def test_rate_limit_does_not_open_circuit(breaker_clock):
    breaker, _ = breaker_clock
    provider = SimpleNamespace(complete=AsyncMock(side_effect=[
        RetryableProviderError("limited", status_code=429, failure_type="http_429"),
        GenerationResult("ok", "fake-fast"),
    ]))
    result = asyncio.run(generate(provider, breaker))
    assert result.attempts == 2
    assert breaker.state == "closed"


# Cancelling a half-open probe releases its permit and allows later recovery.
def test_cancelled_probe_can_recover_after_cooldown(breaker_clock, monkeypatch):
    breaker, now = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    now[0] = 30
    log = Mock()
    monkeypatch.setattr(generation, "log_event", log)

    # Cancels an in-flight probe and checks that another probe can later succeed.
    async def scenario():
        entered = asyncio.Event()

        # Waits until cancelled, simulating an interrupted remote request.
        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        provider = SimpleNamespace(complete=AsyncMock(side_effect=blocked))
        task = asyncio.create_task(generate(provider, breaker))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert breaker.state == "open"

        now[0] = 60
        provider.complete = AsyncMock(return_value=GenerationResult("ok", "fake-fast"))
        result = await generate(provider, breaker)
        assert result.attempts == 1
        assert breaker.state == "closed"

    asyncio.run(scenario())
    transitions = [
        (call.kwargs["previous_state"], call.kwargs["state"])
        for call in log.call_args_list
        if call.args == ("circuit_state_changed",)
    ]
    assert transitions == [
        ("open", "half_open"), ("half_open", "open"),
        ("open", "half_open"), ("half_open", "closed"),
    ]


# Unexpected, permanent, and explicitly uncertain failures release probe admission.
@pytest.mark.parametrize("error", [
    RuntimeError("local bug"),
    PermanentProviderError("unauthorized"),
    ProviderOutcomeUnknown("response lost"),
])
def test_non_outage_errors_release_probe(breaker_clock, error):
    breaker, now = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    now[0] = 30
    provider = SimpleNamespace(complete=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        asyncio.run(generate(provider, breaker))
    assert breaker.state == "open"


# The original request deadline prevents fallback after a slow failed attempt.
def test_deadline_is_not_reset_for_fallback(breaker_clock, monkeypatch):
    breaker, _ = breaker_clock
    now = [0.0]
    monkeypatch.setattr(generation, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: now[0]),
        TimeoutError=asyncio.TimeoutError,
        sleep=asyncio.sleep,
    ))

    # Advances beyond the deadline before reporting an uncertain failure.
    async def slow_failure(**kwargs):
        now[0] = 6.0
        raise RetryableProviderError("timeout", failure_type="timeout", outcome_unknown=True)

    primary = SimpleNamespace(complete=AsyncMock(side_effect=slow_failure))
    fallback = SimpleNamespace(complete=AsyncMock())
    with pytest.raises(RetryableProviderError) as error:
        asyncio.run(generate(
            primary, breaker,
            request_deadline_seconds=5,
            fallback_provider=fallback,
            fallback_provider_name="other",
            fallback_circuit_breaker=CircuitBreaker(),
        ))
    assert error.value.failure_type == "deadline_exceeded"
    assert error.value.attempts == 1
    assert error.value.outcome_unknown is True
    fallback.complete.assert_not_awaited()
