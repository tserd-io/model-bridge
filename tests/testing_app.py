"""Explicit test entry point using fake generation and simulated identity."""

from model_bridge.api.dependencies import get_authenticated_tenant
import os

from fastapi import FastAPI

from model_bridge.config.loader import SETTINGS
from model_bridge.api.dependencies import AuthenticatedTenant


# Supplies a trusted identity for this isolated test application.
async def authenticated_test_tenant() -> AuthenticatedTenant:
    return AuthenticatedTenant(id="test-tenant")


# Enables simulated authentication only through the explicit test entry point.
def create_test_app() -> FastAPI:
    if SETTINGS.primary_provider != "fake" or SETTINGS.fallback_provider is not None:
        raise RuntimeError(
            "The test application requires a fake provider and no fallback"
        )

    if not os.environ.get("IDEMPOTENCY_DB_PATH"):
        raise RuntimeError(
            "The test application requires explicitly configured test storage"
        )

    # Import after checking settings because main initializes providers/storage.
    from model_bridge.main import create_app

    app = create_app()
    app.dependency_overrides[get_authenticated_tenant] = (
        authenticated_test_tenant
    )
    return app
