"""Health probes tested against temporary databases, without model calls."""

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def health_app(monkeypatch, tmp_path):
    database_path = tmp_path / "requests.sqlite3"
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("IDEMPOTENCY_DB_PATH", str(database_path))

    import scripts.main as main
    from scripts.request_store import RequestStore

    monkeypatch.setattr(main, "request_store", RequestStore(database_path))

    async def unexpected_model_call(*args, **kwargs):
        pytest.fail("Health probes must not call a model")

    monkeypatch.setattr(main, "llm_call", unexpected_model_call)
    with TestClient(main.app) as client:
        yield main, client


def test_liveness_does_not_access_database(health_app, monkeypatch):
    main, client = health_app

    def unexpected_database_check():
        pytest.fail("Liveness must not depend on database availability")

    monkeypatch.setattr(main, "_check_database", unexpected_database_check)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_with_healthy_database(health_app):
    _, client = health_app
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


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
        main, "request_store", SimpleNamespace(database_path=database_path)
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready", "checks": {"database": "failed"}
    }
    assert client.get("/health/live").status_code == 200
    if database_state == "missing":
        assert not database_path.exists()


def test_readiness_recovers_after_database_returns(health_app, monkeypatch, tmp_path):
    main, client = health_app
    healthy_store = main.request_store
    monkeypatch.setattr(
        main,
        "request_store",
        SimpleNamespace(database_path=tmp_path / "missing.sqlite3"),
    )
    assert client.get("/health/ready").status_code == 503
    monkeypatch.setattr(main, "request_store", healthy_store)
    assert client.get("/health/ready").status_code == 200
