"""Explicit fixtures; pure unit tests need no per-test app or database."""

import os
from tempfile import TemporaryDirectory

# Protect developer storage and credentials before application imports during collection.
_collection_storage = TemporaryDirectory(prefix="model-bridge-collection-")
os.environ["LLM_PROVIDER"] = "fake"
os.environ["LLM_FALLBACK_PROVIDER"] = ""
os.environ["IDEMPOTENCY_DB_PATH"] = os.path.join(
    _collection_storage.name, "import.sqlite3"
)

import pytest
from unittest.mock import AsyncMock
from model_bridge import main
from model_bridge.api.dependencies import AuthenticatedTenant, get_authenticated_tenant
from model_bridge.providers.contracts import GenerationResult
from tests.support.harness import build_harness, test_settings as make_settings


# Creates a complete application only when explicitly requested by a scenario.
@pytest.fixture
def app_factory(tmp_path, preserve_logging):
    count = 0

    def build(**options):
        nonlocal count
        count += 1
        return build_harness(tmp_path / f"acceptance-{count}.sqlite3", **options)

    return build


# Supplies a real service without modifying main.app.
@pytest.fixture
def isolated_chat_service(tmp_path):
    return main.create_chat_service(make_settings(tmp_path / "requests.sqlite3"))


# Supplies a fresh app and explicit identity to focused HTTP tests.
@pytest.fixture
def test_app(isolated_chat_service, preserve_logging):
    app = main.create_app(service=isolated_chat_service)
    app.dependency_overrides[get_authenticated_tenant] = lambda: AuthenticatedTenant(
        "test-tenant"
    )
    return app


# Keeps narrow generation mocks in focused policy tests, not acceptance tests.
@pytest.fixture
def policy_service(test_app, tmp_path):
    count = 0

    def build(*, tenant=None, overrides=None, platform=None, storage=None):
        nonlocal count
        count += 1
        settings = make_settings(
            tmp_path / f"policy-{count}.sqlite3",
            tenant=tenant,
            overrides=overrides,
            platform=platform,
            storage=storage,
        )
        service = main.create_chat_service(settings)
        service.generate = AsyncMock(return_value=GenerationResult("ok", "fake-fast"))
        test_app.state.chat_service = service
        return service

    return build


# Restores logger configuration after tests that construct applications.
@pytest.fixture
def preserve_logging():
    from model_bridge.observability.logging import logger

    level, disabled, handlers = logger.level, logger.disabled, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.disabled = disabled
    logger.handlers = handlers


# Removes only the disposable import-time database after the session.
def pytest_sessionfinish(session, exitstatus):
    _collection_storage.cleanup()
