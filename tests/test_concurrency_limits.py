"""Capacity admission must be immediate, bounded, and released on every exit."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Barrier, Event
from unittest.mock import AsyncMock

import httpx
import pytest

from model_bridge import main
from model_bridge.providers.contracts import GenerationResult
from model_bridge.providers.contracts import PermanentProviderError
from model_bridge.execution.rate_limit import TenantRateLimiter
from model_bridge.storage.request_store import RequestStore


# Loads the limiter interface used by capacity checks.
def limiter_types():
    module = import_module("model_bridge.execution.concurrency_limit")
    return module.GenerationConcurrencyLimiter, module.ConcurrencyLimitExceeded


# Checks invalid capacities fail at construction rather than during admission.
@pytest.mark.parametrize("limit", [0, -1])
def test_concurrency_limit_must_be_positive(limit):
    limiter_class, _ = limiter_types()
    with pytest.raises(ValueError):
        limiter_class(limit)


# Checks full capacity rejects immediately and a completed job frees its slot.
def test_full_capacity_rejects_and_then_recovers():
    limiter_class, rejected = limiter_types()
    limiter = limiter_class(1)
    with limiter.slot():
        with pytest.raises(rejected):
            with limiter.slot():
                pytest.fail("Excess generation was admitted")
    with limiter.slot():
        pass


# Checks exceptions release capacity instead of leaking a slot permanently.
def test_exception_releases_generation_slot():
    limiter_class, _ = limiter_types()
    limiter = limiter_class(1)
    with pytest.raises(RuntimeError, match="simulated failure"):
        with limiter.slot():
            raise RuntimeError("simulated failure")
    with limiter.slot():
        pass


# Checks task cancellation also releases the slot through context-manager cleanup.
def test_cancellation_releases_generation_slot():
    limiter_class, _ = limiter_types()
    limiter = limiter_class(1)

    async def scenario():
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def hold_slot():
            with limiter.slot():
                entered.set()
                await blocked.wait()

        task = asyncio.create_task(hold_slot())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with limiter.slot():
            pass

    asyncio.run(scenario())


# Checks simultaneous thread admission cannot exceed the configured capacity.
def test_simultaneous_generation_admission_is_bounded():
    limiter_class, rejected = limiter_types()
    limiter = limiter_class(2)
    start = Barrier(8)
    admitted = []
    release = Event()

    def acquire():
        start.wait(timeout=3)
        try:
            with limiter.slot():
                admitted.append(True)
                if not release.wait(timeout=3):
                    raise TimeoutError("Test did not release admitted jobs")
                return True
        except rejected:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(acquire) for _ in range(8)]
        try:
            # Rejections must complete while admitted jobs remain blocked.
            from concurrent.futures import wait, FIRST_COMPLETED

            pending = set(futures)
            for _ in range(6):
                done, pending = wait(pending, timeout=2, return_when=FIRST_COMPLETED)
                assert done, "Excess jobs waited instead of being rejected"
                assert all(future.result() is False for future in done)
                if len(pending) == 2:
                    break
            assert len(pending) == 2
            assert len(admitted) == 2
        finally:
            release.set()
        assert sum(future.result() for future in futures) == 2


# Isolates API storage and admission rate while leaving generation capacity under test.
@pytest.fixture
def concurrency_app(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main.app.state.chat_service,
        "request_store",
        RequestStore(tmp_path / "requests.sqlite3"),
    )
    monkeypatch.setattr(
        main.app.state.chat_service, "chat_rate_limiter", TenantRateLimiter(100, 60)
    )
    return main


# Returns a valid payload with a distinct idempotency identity for each job.
def payload(request_id):
    return {
        "request_id": request_id,
        "message": "hello",
        "model_preference": "fast",
        "max_tokens": 10,
    }


# Checks saturation produces zero-attempt 503 and the rejected ID can later succeed.
def test_api_capacity_rejection_preserves_retryable_identity(
    concurrency_app, monkeypatch
):
    limiter_class, _ = limiter_types()
    limiter = limiter_class(1)
    monkeypatch.setattr(
        concurrency_app.app.state.chat_service,
        "generation_limiter",
        limiter,
        raising=False,
    )
    generation = AsyncMock(return_value=GenerationResult("ok", "fake-fast", attempts=1))
    monkeypatch.setattr(concurrency_app.app.state.chat_service, "generate", generation)

    async def scenario():
        transport = httpx.ASGITransport(app=concurrency_app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            with limiter.slot():
                response = await client.post("/chat", json=payload("retry-later"))
            assert response.status_code == 503
            assert int(response.headers["Retry-After"]) >= 1
            assert response.json()["attempts"] == 0
            generation.assert_not_awaited()
            response = await client.post("/chat", json=payload("retry-later"))
            assert response.status_code == 200
            generation.assert_awaited_once()

    asyncio.run(scenario())


# Checks cached replay needs no generation slot even while capacity is exhausted.
def test_cached_replay_is_available_at_full_capacity(concurrency_app, monkeypatch):
    limiter_class, _ = limiter_types()
    limiter = limiter_class(1)
    monkeypatch.setattr(
        concurrency_app.app.state.chat_service,
        "generation_limiter",
        limiter,
        raising=False,
    )
    generation = AsyncMock(return_value=GenerationResult("ok", "fake-fast", attempts=1))
    monkeypatch.setattr(concurrency_app.app.state.chat_service, "generate", generation)

    async def scenario():
        transport = httpx.ASGITransport(app=concurrency_app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            assert (
                await client.post("/chat", json=payload("cached"))
            ).status_code == 200
            with limiter.slot():
                response = await client.post("/chat", json=payload("cached"))
            assert response.status_code == 200
            assert response.json()["cache_hit"] is True
            generation.assert_awaited_once()

    asyncio.run(scenario())


# Checks the real API acquires a slot and releases it after a provider rejection.
def test_api_provider_failure_releases_capacity(concurrency_app, monkeypatch):
    limiter_class, rejected = limiter_types()
    limiter = limiter_class(1)
    monkeypatch.setattr(
        concurrency_app.app.state.chat_service,
        "generation_limiter",
        limiter,
        raising=False,
    )

    async def fail_generation(**kwargs):
        with pytest.raises(rejected):
            with limiter.slot():
                pytest.fail("API did not acquire generation capacity")
        raise PermanentProviderError("simulated rejection", attempts=1)

    monkeypatch.setattr(
        concurrency_app.app.state.chat_service, "generate", fail_generation
    )

    async def scenario():
        transport = httpx.ASGITransport(app=concurrency_app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post("/chat", json=payload("provider-failure"))
            assert response.status_code == 502
        with limiter.slot():
            pass

    asyncio.run(scenario())
