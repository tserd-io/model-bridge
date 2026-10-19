"""Admission and input boundaries observed through complete requests."""

import asyncio
import httpx
import pytest
from tests.support.harness import payload
from tests.support.providers import BlockingProvider

pytestmark = pytest.mark.acceptance


# The tenant's token boundary rejects oversized work before any provider call.
@pytest.mark.parametrize("tokens,status", [(16, 200), (17, 422)])
def test_token_limit(app_factory, tokens, status):
    gateway = app_factory(tenant={"max_output_tokens": 16})
    with gateway.client() as client:
        response = client.post("/chat", json=payload(max_tokens=tokens))
    assert response.status_code == status
    assert len(gateway.primary.calls) == (1 if status == 200 else 0)


# Oversized request bytes are rejected before generation even for otherwise valid JSON.
def test_body_limit(app_factory):
    gateway = app_factory(tenant={"max_input_bytes": 256})
    with gateway.client() as client:
        response = client.post("/chat", json=payload(message="x" * 300))
    assert response.status_code == 413
    assert gateway.primary.calls == []


# Rate rejection exposes retry guidance while another tenant retains its own quota.
def test_tenant_rate_limit(app_factory):
    gateway = app_factory(tenant={"rate_limit": {"requests": 1, "window_seconds": 60}})
    with gateway.client() as client:
        first = client.post("/chat", json=payload("first"))
        rejected = client.post("/chat", json=payload("second"))
        other = client.post(
            "/chat", headers={"x-test-tenant": "other"}, json=payload("third")
        )
    assert first.status_code == other.status_code == 200
    assert rejected.status_code == 429
    assert int(rejected.headers["Retry-After"]) > 0
    assert len(gateway.primary.calls) == 2


# A real in-flight generation holds capacity; its completion allows retry of the rejected ID.
def test_capacity_rejection_and_recovery(app_factory):
    async def scenario():
        provider = BlockingProvider()
        gateway = app_factory(primary=provider, tenant={"max_concurrent_jobs": 1})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
        ) as client:
            first = asyncio.create_task(client.post("/chat", json=payload("running")))
            try:
                await asyncio.wait_for(provider.entered.wait(), timeout=2)
                rejected = await client.post("/chat", json=payload("retry"))
                assert rejected.status_code == 503
                assert rejected.json()["attempts"] == 0
                assert rejected.headers["Retry-After"] == "1"
                provider.release.set()
                assert (await asyncio.wait_for(first, timeout=2)).status_code == 200
                assert (
                    await client.post("/chat", json=payload("retry"))
                ).status_code == 200
                assert len(provider.calls) == 2
            finally:
                provider.release.set()
                if not first.done():
                    first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    asyncio.run(scenario())
