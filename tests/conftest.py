from model_bridge.api.dependencies import get_authenticated_tenant
import pytest

from model_bridge import main
from model_bridge.api.dependencies import AuthenticatedTenant
from model_bridge.config.loader import SETTINGS
from model_bridge.config.models import Settings
from unittest.mock import AsyncMock
from model_bridge.providers.contracts import GenerationResult


# Gives every test a fresh service, fake provider, admission state and temporary storage.
@pytest.fixture(autouse=True)
def isolated_chat_service(monkeypatch, tmp_path):
    data = SETTINGS.model_dump()
    data["providers"]["fake"] = {"type": "fake"}
    data["primary_provider"] = "fake"
    data["fallback_provider"] = None
    data["storage"]["path"] = tmp_path / "requests.sqlite3"
    service = main.create_chat_service(Settings.model_validate(data))
    monkeypatch.setattr(main.app.state, "chat_service", service)
    return service


# Supplies trusted identity for ordinary API tests without real credentials.
@pytest.fixture(autouse=True)
def default_authenticated_identity(monkeypatch):
    async def authenticated_for_test():
        return AuthenticatedTenant(id="test-tenant")

    monkeypatch.setitem(
        main.app.dependency_overrides,
        get_authenticated_tenant,
        authenticated_for_test,
    )


# Builds policy-specific services with fake generation and independent temporary storage.
@pytest.fixture
def policy_service(monkeypatch, tmp_path):
    count = 0

    # Applies selected policy changes without mutating the application's global settings.
    def build(*, tenant=None, overrides=None, platform=None, storage=None):
        nonlocal count
        count += 1
        data = SETTINGS.model_dump()
        data["providers"]["fake"] = {"type": "fake"}
        data["primary_provider"] = "fake"
        data["fallback_provider"] = None
        data["platform"]["rate_limit"] = {"requests": 100, "window_seconds": 60}
        data["tenant_defaults"]["rate_limit"] = {"requests": 100, "window_seconds": 60}
        data["tenant_overrides"] = {}
        data["tenant_defaults"].update(tenant or {})
        data["tenant_overrides"].update(overrides or {})
        data["platform"].update(platform or {})
        data["storage"].update(storage or {})
        data["storage"]["path"] = tmp_path / f"policy-{count}.sqlite3"
        service = main.create_chat_service(Settings.model_validate(data))
        service.generate = AsyncMock(return_value=GenerationResult("ok", "fake-fast"))
        monkeypatch.setattr(main.app.state, "chat_service", service)
        return service

    return build
