"""Health probes tested against temporary databases, without model calls."""

import sqlite3
import time
import pytest
from fastapi.testclient import TestClient


# Creates isolated request storage and a client while forbidding model calls from health probes.
@pytest.fixture
def health_app(monkeypatch, tmp_path):
    database_path = tmp_path / "requests.sqlite3"
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("IDEMPOTENCY_DB_PATH", str(database_path))

    from model_bridge import main
    from model_bridge.storage.request_store import RequestStore

    monkeypatch.setattr(
        main.app.state.chat_service, "request_store", RequestStore(database_path)
    )

    # Fails immediately if a health probe attempts generation.
    async def unexpected_model_call(*args, **kwargs):
        pytest.fail("Health probes must not call a model")

    monkeypatch.setattr(main.app.state.chat_service, "generate", unexpected_model_call)
    with TestClient(main.app) as client:
        yield main, client


# Checks that liveness responds successfully without accessing the database.
def test_liveness_does_not_access_database(health_app, monkeypatch):
    main, client = health_app

    # Fails immediately if liveness attempts to inspect the database.
    def unexpected_database_check():
        pytest.fail("Liveness must not depend on database availability")

    monkeypatch.setattr(
        main.app.state.chat_service.request_store,
        "check_readable",
        unexpected_database_check,
    )
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


# Checks that readiness returns HTTP 200 when the requests table is readable.
def test_readiness_with_healthy_database(health_app):
    _, client = health_app
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


# Checks HTTP 503 for missing, corrupt, or schema-less databases while liveness remains available.
@pytest.mark.parametrize("database_state", ["missing", "corrupt", "missing_schema"])
def test_readiness_with_unavailable_database(
    health_app, monkeypatch, tmp_path, database_state
):
    main, client = health_app
    database_path = tmp_path / "unavailable.sqlite3"
    if database_state == "corrupt":
        database_path.write_bytes(b"This is not a SQLite database.")
    elif database_state == "missing_schema":
        sqlite3.connect(database_path).close()

    monkeypatch.setattr(
        main.app.state.chat_service.request_store,
        "database_path",
        database_path,
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}
    assert client.get("/health/live").status_code == 200
    if database_state == "missing":
        assert not database_path.exists()


# Checks that readiness recovers after database access is restored.
def test_readiness_recovers_after_database_returns(health_app, monkeypatch, tmp_path):
    main, client = health_app
    healthy_path = main.app.state.chat_service.request_store.database_path
    monkeypatch.setattr(
        main.app.state.chat_service.request_store,
        "database_path",
        tmp_path / "missing.sqlite3",
    )
    assert client.get("/health/ready").status_code == 503
    monkeypatch.setattr(
        main.app.state.chat_service.request_store,
        "database_path",
        healthy_path,
    )
    assert client.get("/health/ready").status_code == 200


# Checks a long normal-storage timeout does not make readiness wait for that full period.
def test_readiness_timeout_is_independent_of_storage_busy_timeout(policy_service):
    service = policy_service(storage={"busy_timeout_seconds": 30})
    from model_bridge import main

    blocker = sqlite3.connect(service.request_store.database_path)
    try:
        # DELETE journaling and an exclusive lock block readers, unlike an ordinary WAL writer.
        blocker.execute("PRAGMA journal_mode=DELETE")
        blocker.execute("BEGIN EXCLUSIVE")
        with TestClient(main.app) as client:
            started = time.monotonic()
            response = client.get("/health/ready")
            elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert elapsed < 3
    finally:
        blocker.rollback()
        blocker.close()
