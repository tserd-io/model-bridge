"""The application workflow runs directly without HTTP clients or schemas."""

import asyncio
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from types import SimpleNamespace
import pytest
from model_bridge.api.responses import to_http_response
from model_bridge.execution import generation
from model_bridge.providers.contracts import GenerationResult
from model_bridge.providers.contracts import PermanentProviderError, RetryableProviderError
import model_bridge.storage.request_store as storage_module
from model_bridge.application.outcomes import ChatCommand, ChatOutcome
from model_bridge.execution.rate_limit import TenantRateLimiter


def test_service_in_progress_response_preserves_retry_after(policy_service):
    service = policy_service()
    command = ChatCommand("pending-metadata", "test-tenant", "hello", "fast", 10)
    claimed = service.request_store.claim(
        command.tenant_id, command.request_id, asdict(command), 60
    )
    assert claimed.status == "claimed"

    outcome = asyncio.run(service.handle(command))
    assert outcome.kind == "in_progress"
    assert outcome.retry_after == 1
    response = to_http_response(outcome)
    assert response.status_code == 202
    assert response.headers["Retry-After"] == "1"
    service.generate.assert_not_awaited()


@pytest.mark.parametrize(
    "failure, expected_kind, expected_error, expected_timeout",
    [
        (asyncio.TimeoutError(), "unknown", "timeout", True),
        (
            RetryableProviderError(
                "deadline expired", attempts=0, failure_type="deadline_exceeded"
            ),
            "failed", "deadline_exceeded", True,
        ),
        (
            PermanentProviderError("rejected", attempts=1),
            "provider_rejected", "provider_rejection", False,
        ),
    ],
)
def test_service_preserves_error_and_timeout_metadata(
    policy_service, failure, expected_kind, expected_error, expected_timeout
):
    service = policy_service()
    service.generate.side_effect = failure
    command = ChatCommand("error-metadata", "test-tenant", "hello", "fast", 10)

    outcome = asyncio.run(service.handle(command))

    assert outcome.kind == expected_kind
    assert outcome.error_type == expected_error
    assert outcome.timed_out is expected_timeout
    assert outcome.retry_after is None
    service.generate.assert_awaited_once()


# Verifies direct service execution isolates tenants sharing an ID and replays saved results.
def test_service_handles_tenant_scoped_commands_without_http(isolated_chat_service):
    service = isolated_chat_service
    service.chat_rate_limiter = TenantRateLimiter(100, 60)
    command_a = ChatCommand("shared-id", "tenant-a", "first", "fast", 10)
    command_b = ChatCommand("shared-id", "tenant-b", "second", "fast", 10)

    async def scenario():
        first = await service.handle(command_a)
        second = await service.handle(command_b)
        replay = await service.handle(command_a)
        assert isinstance(first, ChatOutcome)
        assert first.kind == second.kind == replay.kind == "success"
        assert first.response.content == "Fake response: first"
        assert second.response.content == "Fake response: second"
        assert second.response.tenant_id == "tenant-b"
        assert replay.replayed is True
        assert replay.response.cache_hit is True
        assert replay.response.content == first.response.content

    asyncio.run(scenario())


# Checks staggered tenant requests receive independent generation budgets and cancellation.
def test_staggered_tenant_generation_deadlines(policy_service, monkeypatch):
    service = policy_service(
        tenant={"request_deadline_seconds": 2},
        overrides={"b": {"request_deadline_seconds": 5}},
    )
    service.generate = generation.llm_call
    service.max_attempts = 1
    now = [0.0]
    monkeypatch.setattr(
        generation,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: now[0]),
            TimeoutError=asyncio.TimeoutError,
            sleep=asyncio.sleep,
        ),
    )

    # Coordinates provider entry and completion so requests truly overlap without real sleeps.
    async def scenario():
        entered = {name: asyncio.Event() for name in ("a", "b")}
        release = {name: asyncio.Event() for name in ("a", "b")}
        timeouts = {}
        cancelled = []

        # Simulates provider-enforced timeout for A while B remains pending.
        async def complete(**kwargs):
            name = kwargs["request_id"]
            timeouts[name] = kwargs["timeout"]
            entered[name].set()
            try:
                await release[name].wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise
            if name == "a":
                raise asyncio.TimeoutError()
            return GenerationResult("b completed", "fake-fast")

        service.primary_provider = SimpleNamespace(complete=complete)
        tasks = []
        try:
            tasks.append(
                asyncio.create_task(
                    service.handle(ChatCommand("a", "a", "hello", "fast", 10))
                )
            )
            await asyncio.wait_for(entered["a"].wait(), 2)
            now[0] = 1
            tasks.append(
                asyncio.create_task(
                    service.handle(ChatCommand("b", "b", "hello", "fast", 10))
                )
            )
            await asyncio.wait_for(entered["b"].wait(), 2)
            now[0] = 2
            release["a"].set()
            first = await asyncio.wait_for(tasks[0], 2)
            assert first.kind == "unknown"
            assert not tasks[1].done()
            assert cancelled == []
            now[0] = 3
            release["b"].set()
            second = await asyncio.wait_for(tasks[1], 2)
            assert second.kind == "success"
            assert timeouts == {"a": 2, "b": 5}
            assert service.generation_limiter._active == 0
            assert service.generation_limiter._tenant_active == {}
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


# Checks configured busy timeout reaches SQLite and bounds a blocked write.
@pytest.mark.parametrize("seconds", [0.1, 0.3])
def test_configured_sqlite_busy_timeout(policy_service, seconds):
    service = policy_service(storage={"busy_timeout_seconds": seconds})
    store = service.request_store
    with store._connection() as connection:
        configured_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert configured_ms == pytest.approx(seconds * 1000, abs=1)
    with closing(sqlite3.connect(store.database_path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store.claim("a", "blocked", {"message": "hello"}, 10)
            elapsed = time.monotonic() - started
        finally:
            blocker.rollback()
    assert seconds * 0.7 <= elapsed <= seconds + 0.8


# Checks tenant deadline plus configured margin governs expiry when a record is checked again.
def test_configured_processing_lease(policy_service, monkeypatch):
    service = policy_service(
        tenant={"request_deadline_seconds": 10}, storage={"lease_margin_seconds": 3}
    )
    now = [100.0]
    monkeypatch.setattr(storage_module, "time", SimpleNamespace(time=lambda: now[0]))
    original_claim = service.request_store.claim
    command = ChatCommand("lease", "a", "hello", "fast", 10)

    # Records the real service lease while modelling a worker that never finishes generation.
    def abandoned_claim(tenant, rid, data, lease):
        original_claim(tenant, rid, data, lease)
        return storage_module.RequestRecord(status="in_progress")

    monkeypatch.setattr(service.request_store, "claim", abandoned_claim)
    result = asyncio.run(service.handle(command))
    assert result.kind == "in_progress"
    with closing(sqlite3.connect(service.request_store.database_path)) as connection:
        lease = connection.execute(
            "SELECT lease_expires_at FROM requests WHERE request_id='lease'"
        ).fetchone()[0]
    assert lease == 113
    from dataclasses import asdict

    now[0] = 112.9
    assert original_claim("a", "lease", asdict(command), 13).status == "in_progress"
    now[0] = 113
    assert original_claim("a", "lease", asdict(command), 13).status == "unknown"
