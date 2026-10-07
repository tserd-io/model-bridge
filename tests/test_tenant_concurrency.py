"""Tenant admission must use trusted identity and respect both capacity ceilings.

These tests exercise the slot(tenant_id=..., tenant_limit=...) interface
and a get_authenticated_tenant FastAPI dependency returning an object
with an id attribute. The test-only header below replaces that dependency;
it must never become a production authentication mechanism.
"""

from model_bridge.api.dependencies import get_authenticated_tenant
import asyncio
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from importlib import import_module
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request

from model_bridge import main
from model_bridge.providers.contracts import GenerationResult
from model_bridge.config.loader import SETTINGS
from model_bridge.execution.rate_limit import TenantRateLimiter
from model_bridge.storage.request_store import RequestStore
from model_bridge.config.models import Settings


# Loads the limiter interface used by tenant admission checks.
def limiter_types():
    module = import_module("model_bridge.execution.concurrency_limit")
    return module.GenerationConcurrencyLimiter, module.ConcurrencyLimitExceeded


# Checks one saturated tenant cannot consume another tenant's remaining allowance.
def test_tenant_saturation_leaves_other_tenants_available():
    limiter_class, rejected = limiter_types()
    limiter = limiter_class(3)
    with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
        with pytest.raises(rejected):
            with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
                pytest.fail("Tenant allowance was exceeded")
        with limiter.slot(tenant_id="tenant-b", tenant_limit=2):
            with limiter.slot(tenant_id="tenant-b", tenant_limit=2):
                pass


# Checks the platform ceiling rejects every tenant when aggregate capacity is full.
@pytest.mark.parametrize("next_tenant", ["tenant-a", "tenant-b", "tenant-c"])
def test_platform_saturation_blocks_all_tenants(next_tenant):
    limiter_class, rejected = limiter_types()
    limiter = limiter_class(2)
    with limiter.slot(tenant_id="tenant-a", tenant_limit=2):
        with limiter.slot(tenant_id="tenant-b", tenant_limit=2):
            with pytest.raises(rejected):
                with limiter.slot(tenant_id=next_tenant, tenant_limit=2):
                    pytest.fail("Platform capacity was exceeded")


# Checks a rejected tenant admission cannot leak a platform or tenant slot.
def test_rejected_tenant_admission_does_not_consume_capacity():
    limiter_class, rejected = limiter_types()
    limiter = limiter_class(2)
    with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
        for _ in range(3):
            with pytest.raises(rejected):
                with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
                    pytest.fail("Tenant allowance was exceeded")
        with limiter.slot(tenant_id="tenant-b", tenant_limit=1):
            pass
    with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
        with limiter.slot(tenant_id="tenant-b", tenant_limit=1):
            pass


# Checks provider-style errors release both tenant and platform capacity.
def test_exception_releases_both_capacity_counts():
    limiter_class, _ = limiter_types()
    limiter = limiter_class(2)
    with pytest.raises(RuntimeError, match="generation failed"):
        with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
            raise RuntimeError("generation failed")
    with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
        with limiter.slot(tenant_id="tenant-b", tenant_limit=1):
            pass


# Checks cancellation releases the cancelled tenant's allowance and global capacity.
def test_cancellation_releases_both_capacity_counts():
    limiter_class, _ = limiter_types()
    limiter = limiter_class(2)

    # Cancels an admitted job and then fills both platform slots to detect leaks.
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        # Holds an authenticated tenant's slot until the task is cancelled.
        async def hold():
            with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
                entered.set()
                await release.wait()

        task = asyncio.create_task(hold())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
            with limiter.slot(tenant_id="tenant-b", tenant_limit=1):
                pass

    asyncio.run(scenario())


# Checks simultaneous admission enforces tenant and platform ceilings atomically.
def test_simultaneous_tenant_admission_respects_both_ceilings():
    limiter_class, rejected = limiter_types()
    limiter = limiter_class(3)
    start = Barrier(8)
    release = Event()
    tenants = ["tenant-a"] * 4 + ["tenant-b"] * 4

    # Keeps successful jobs active while excess admissions must finish with rejection.
    def acquire(tenant):
        start.wait(timeout=5)
        try:
            with limiter.slot(tenant_id=tenant, tenant_limit=2):
                if not release.wait(timeout=10):
                    raise TimeoutError("Test did not release admitted jobs")
                return tenant
        except rejected:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(acquire, tenant) for tenant in tenants]
        pending = set(futures)
        try:
            rejected_count = 0
            while rejected_count < 5:
                done, pending = wait(pending, timeout=3, return_when=FIRST_COMPLETED)
                assert done, "Excess work waited instead of being rejected"
                assert all(future.result() is None for future in done)
                rejected_count += len(done)
            assert rejected_count == 5
            assert len(pending) == 3
        finally:
            release.set()
        admitted = Counter(future.result() for future in futures)
    assert admitted[None] == 5
    assert admitted["tenant-a"] + admitted["tenant-b"] == 3
    assert 1 <= admitted["tenant-a"] <= 2
    assert 1 <= admitted["tenant-b"] <= 2


# Checks zero or negative tenant allowances cannot enter or leak platform capacity.
@pytest.mark.parametrize("tenant_limit", [0, -1])
def test_invalid_tenant_allowance_is_rejected(tenant_limit):
    limiter_class, _ = limiter_types()
    limiter = limiter_class(1)
    with pytest.raises(ValueError):
        with limiter.slot(tenant_id="tenant-a", tenant_limit=tenant_limit):
            pytest.fail("Invalid tenant allowance was accepted")
    with limiter.slot(tenant_id="tenant-b", tenant_limit=1):
        pass


# Supplies deterministic tenant policies without depending on production config values.
@pytest.fixture
def tenant_api(monkeypatch, tmp_path):
    data = SETTINGS.model_dump()
    data["platform"]["max_concurrent_jobs_per_instance"] = 3
    data["tenant_defaults"]["max_concurrent_jobs"] = 2
    data["tenant_overrides"] = {"tenant-a": {"max_concurrent_jobs": 1}}
    settings = Settings.model_validate(data)
    monkeypatch.setattr(
        main.app.state.chat_service, "settings", settings, raising=False
    )
    monkeypatch.setattr(
        main.app.state.chat_service,
        "request_store",
        RequestStore(tmp_path / "requests.sqlite3"),
    )
    monkeypatch.setattr(
        main.app.state.chat_service, "chat_rate_limiter", TenantRateLimiter(100, 60)
    )
    generation = AsyncMock(
        return_value=GenerationResult(
            "ok",
            "fake-fast",
            attempts=1,
            provider="fake",
        )
    )
    monkeypatch.setattr(main.app.state.chat_service, "generate", generation)
    return settings, generation


# Replaces future authentication with trusted test identities; no real credentials are used.
def configure_identity(monkeypatch):
    dependency = get_authenticated_tenant
    assert callable(dependency), "Wire a get_authenticated_tenant dependency into /chat"

    # Simulates a trusted authenticator's result, not a production header-based login.
    async def authenticated_for_test(request: Request):
        tenant_id = request.headers.get("x-test-authenticated-tenant")
        if tenant_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return SimpleNamespace(id=tenant_id)

    monkeypatch.setitem(
        main.app.dependency_overrides, dependency, authenticated_for_test
    )


# Keeps caller-supplied tenant metadata separate from the trusted authentication result.
def payload(request_id, claimed_tenant):
    return {
        "request_id": request_id,
        "tenant_id": claimed_tenant,
        "message": "hello",
        "model_preference": "fast",
        "max_tokens": 10,
    }


# Checks a configured override and the default allowance are enforced by the API.
@pytest.mark.parametrize("tenant, expected_limit", [("tenant-a", 1), ("tenant-b", 2)])
def test_api_resolves_authenticated_tenant_policy(
    tenant_api, monkeypatch, tenant, expected_limit
):
    settings, generation = tenant_api
    limiter_class, _ = limiter_types()
    limiter = limiter_class(3)
    monkeypatch.setattr(
        main.app.state.chat_service, "generation_limiter", limiter, raising=False
    )
    configure_identity(monkeypatch)
    assert (
        settings.effective_tenant_limits(tenant).max_concurrent_jobs == expected_limit
    )

    # Fills only this tenant's allowance, verifies rejection, then retries the same ID.
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            with ExitStack() as held:
                for _ in range(expected_limit):
                    held.enter_context(
                        limiter.slot(tenant_id=tenant, tenant_limit=expected_limit)
                    )
                response = await client.post(
                    "/chat",
                    json=payload("tenant-retry", tenant),
                    headers={"x-test-authenticated-tenant": tenant},
                )
            assert response.status_code == 503
            assert response.json()["attempts"] == 0
            assert int(response.headers["Retry-After"]) >= 1
            generation.assert_not_awaited()
            response = await client.post(
                "/chat",
                json=payload("tenant-retry", tenant),
                headers={"x-test-authenticated-tenant": tenant},
            )
            assert response.status_code == 200
            generation.assert_awaited_once()
            assert generation.await_args.kwargs["tenant_id"] == tenant

    asyncio.run(scenario())


# Checks changing body tenant metadata cannot bypass the authenticated tenant's ceiling.
def test_body_tenant_spoofing_cannot_bypass_capacity(tenant_api, monkeypatch):
    _, generation = tenant_api
    limiter_class, _ = limiter_types()
    limiter = limiter_class(3)
    monkeypatch.setattr(
        main.app.state.chat_service, "generation_limiter", limiter, raising=False
    )
    configure_identity(monkeypatch)

    # Accepts either rejecting a mismatched identity or enforcing authenticated capacity.
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            with limiter.slot(tenant_id="tenant-a", tenant_limit=1):
                for claimed in ("tenant-b", "default", "invented-tenant"):
                    response = await client.post(
                        "/chat",
                        json=payload(f"spoof-{claimed}", claimed),
                        headers={"x-test-authenticated-tenant": "tenant-a"},
                    )
                    assert response.status_code in {403, 503}
            generation.assert_not_awaited()
            response = await client.post(
                "/chat",
                json=payload("honest-b", "tenant-b"),
                headers={"x-test-authenticated-tenant": "tenant-b"},
            )
            assert response.status_code == 200
            assert generation.await_args.kwargs["tenant_id"] == "tenant-b"

    asyncio.run(scenario())


# Checks unauthenticated callers cannot obtain capacity by supplying a body tenant ID.
def test_unauthenticated_request_does_not_generate(tenant_api, monkeypatch):
    _, generation = tenant_api
    configure_identity(monkeypatch)

    # Sends a valid body without an authenticated identity and expects rejection.
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            response = await client.post("/chat", json=payload("anonymous", "tenant-a"))
            assert response.status_code == 401
            generation.assert_not_awaited()

    asyncio.run(scenario())


# Checks same-tenant replay remains available with both platform and tenant slots full.
def test_authenticated_cached_replay_bypasses_capacity(tenant_api, monkeypatch):
    _, generation = tenant_api
    limiter_class, _ = limiter_types()
    limiter = limiter_class(3)
    monkeypatch.setattr(
        main.app.state.chat_service, "generation_limiter", limiter, raising=False
    )
    configure_identity(monkeypatch)

    # Saves a result and then replays it while every generation slot is occupied.
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            headers = {"x-test-authenticated-tenant": "tenant-a"}
            body = payload("tenant-cached", "tenant-a")
            assert (
                await client.post("/chat", json=body, headers=headers)
            ).status_code == 200
            with ExitStack() as held:
                held.enter_context(limiter.slot(tenant_id="tenant-a", tenant_limit=1))
                for _ in range(2):
                    held.enter_context(
                        limiter.slot(tenant_id="tenant-b", tenant_limit=2)
                    )
                response = await client.post("/chat", json=body, headers=headers)
            assert response.status_code == 200
            assert response.json()["cache_hit"] is True
            generation.assert_awaited_once()

    asyncio.run(scenario())
