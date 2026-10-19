"""One factory for fresh settings, providers, identity, storage and HTTP apps."""

from dataclasses import dataclass
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from model_bridge.api.dependencies import AuthenticatedTenant, get_authenticated_tenant
from model_bridge.config.loader import SETTINGS
from model_bridge.config.models import Settings
from model_bridge.main import create_app, create_chat_service
from tests.support.providers import ScriptedProvider


# Supplies predictable quotas while allowing each scenario to change relevant settings.
def test_settings(
    path,
    *,
    tenant=None,
    overrides=None,
    platform=None,
    storage=None,
    fallback=False,
    provider_options=None,
):
    data = SETTINGS.model_dump()
    provider = {
        "type": "fake",
        "retry_base_delay_seconds": 0,
        "max_attempts": 3,
        "attempt_timeout_seconds": 30,
        "circuit_breaker_failure_threshold": 5,
        "circuit_breaker_cooldown_seconds": 30,
    }
    provider.update(provider_options or {})
    data["providers"] = {"fake": provider}
    if fallback:
        data["providers"]["backup"] = {**provider, "type": "fake"}
    data["primary_provider"] = "fake"
    data["fallback_provider"] = "backup" if fallback else None
    data["platform"].update(
        {
            "max_concurrent_jobs_per_instance": 5,
            "rate_limit": {"requests": 100, "window_seconds": 60},
        }
    )
    data["platform"].update(platform or {})
    data["tenant_defaults"]["rate_limit"] = {"requests": 100, "window_seconds": 60}
    data["tenant_defaults"].update(tenant or {})
    data["tenant_overrides"] = overrides or {}
    data["storage"].update(storage or {})
    data["storage"]["path"] = path
    return Settings.model_validate(data)


# Exposes the app and provider boundaries without patching the global application.
@dataclass
class Harness:
    app: FastAPI
    primary: ScriptedProvider
    fallback: ScriptedProvider | None

    @property
    def service(self):
        return self.app.state.chat_service

    def client(self):
        return TestClient(self.app)


# Builds the actual request pipeline with identity simulated only in this test factory.
def build_harness(path, *, primary=None, fallback=None, **settings_options):
    settings = test_settings(path, fallback=fallback is not None, **settings_options)
    service = create_chat_service(settings)
    primary = primary if primary is not None else ScriptedProvider()
    service.primary_provider = primary
    service.backup_provider = fallback
    app = create_app(service=service)

    async def identity(request: Request):
        tenant = request.headers.get("x-test-tenant", "test-tenant")
        if not tenant:
            raise HTTPException(401, "Test identity missing")
        return AuthenticatedTenant(tenant)

    app.dependency_overrides[get_authenticated_tenant] = identity
    return Harness(app, primary, fallback)


# Builds a normal chat request, changing only the fields relevant to a scenario.
def payload(request_id="request-1", **changes):
    return {
        "request_id": request_id,
        "message": "hello",
        "model_preference": "fast",
        "max_tokens": 10,
        **changes,
    }
