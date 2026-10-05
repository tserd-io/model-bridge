import pytest

from scripts import main
from scripts.tenant_identity import AuthenticatedTenant


# Supplies trusted identity for ordinary API tests without real credentials.
@pytest.fixture(autouse=True)
def default_authenticated_identity(monkeypatch):
    async def authenticated_for_test():
        return AuthenticatedTenant(id="test-tenant")

    monkeypatch.setitem(
        main.app.dependency_overrides,
        main.get_authenticated_tenant,
        authenticated_for_test,
    )