from model_bridge.api.dependencies import get_authenticated_tenant
import pytest

from model_bridge import main
from model_bridge.api.dependencies import AuthenticatedTenant
from model_bridge.config.loader import SETTINGS
from model_bridge.config.models import Settings


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
