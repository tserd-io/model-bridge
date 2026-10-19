"""Focused contracts for log configuration and application/server wiring."""

import json
import logging
from pathlib import Path
import re
from unittest.mock import Mock

import pytest
import uvicorn

from model_bridge import main
from model_bridge.observability import logging as event_logging


# Captures application records and restores logger state after each configuration test.
@pytest.fixture
def application_records(monkeypatch):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    monkeypatch.setattr(event_logging.logger, "handlers", [handler])
    original_level = event_logging.logger.level
    monkeypatch.setattr(event_logging.logger, "disabled", event_logging.logger.disabled)
    yield records
    # setLevel also clears the logger's severity cache.
    event_logging.logger.setLevel(original_level)


# Checks the application's logging switch independently of Uvicorn's own logs.
@pytest.mark.parametrize("enabled", [False, True])
def test_application_logging_enabled(policy_service, application_records, enabled):
    service = policy_service(
        platform={"logging": {"enabled": enabled, "level": "info"}}
    )
    main.create_app(settings=service.settings, service=service)
    application_records.clear()
    event_logging.log_event("switch-check", request_id="logging-check")
    events = [
        json.loads(record.getMessage())["event"] for record in application_records
    ]
    assert events == (["switch-check"] if enabled else [])


# Checks explicit event severities are filtered using the configured minimum level.
@pytest.mark.parametrize("minimum", ["debug", "info", "warning", "error", "critical"])
def test_application_log_severity_filter(policy_service, application_records, minimum):
    service = policy_service(platform={"logging": {"enabled": True, "level": minimum}})
    main.create_app(settings=service.settings, service=service)
    application_records.clear()
    levels = ["debug", "info", "warning", "error", "critical"]
    for level in levels:
        event_logging.log_event(level, level=level, request_id="severity-check")
    expected = levels[levels.index(minimum) :]
    assert [
        json.loads(record.getMessage())["event"] for record in application_records
    ] == expected
    assert [record.levelname.lower() for record in application_records] == expected


# Checks real service events retain JSON metadata while omitting private content and secrets.
@pytest.mark.parametrize("fails", [False, True])
def test_application_event_structure_and_privacy(
    policy_service, application_records, monkeypatch, fails
):
    import asyncio
    from unittest.mock import AsyncMock
    from model_bridge.application.outcomes import ChatCommand
    from model_bridge.providers.contracts import (
        GenerationResult,
        PermanentProviderError,
    )

    secret = "secret-credential-for-log-test"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    service = policy_service()
    service.generate = AsyncMock(
        side_effect=PermanentProviderError("provider rejected") if fails else None,
        return_value=GenerationResult("private-answer-marker", "fake-fast"),
    )
    application_records.clear()
    asyncio.run(
        service.handle(
            ChatCommand(
                "privacy-check", "test-tenant", "private-prompt-marker", "fast", 10
            )
        )
    )
    assert application_records
    for record in application_records:
        text = record.getMessage()
        event = json.loads(text)
        assert event["timestamp"].endswith("Z")
        assert event["event"]
        assert event["request_id"] == "privacy-check"
        for private in ("private-answer-marker", "private-prompt-marker", secret):
            assert private not in text


# Checks supported startup passes configured graceful-shutdown time to Uvicorn.
@pytest.mark.parametrize("seconds", [2, 7])
def test_startup_reads_shutdown_grace(policy_service, monkeypatch, seconds):
    service = policy_service(platform={"shutdown_grace_seconds": seconds})
    run_server = getattr(main, "run_server", None)
    assert callable(run_server), (
        "Implement main.run_server(settings, host, port) for configured startup"
    )
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    run_server(settings=service.settings, host="127.0.0.1", port=8000)
    run.assert_called_once()
    assert run.call_args.kwargs["timeout_graceful_shutdown"] == seconds


# Checks the default container command uses configured startup instead of a fixed grace value.
def test_docker_command_uses_configured_server_startup():
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8").replace("\\\n", "")
    command = re.search(r"^CMD\s+(\[.*\])\s*$", text, re.MULTILINE | re.DOTALL)
    assert command is not None, "Use an explicit JSON Docker CMD"
    args = json.loads(command.group(1))
    assert args[:3] == ["python", "-m", "model_bridge.main"]
    assert "--timeout-graceful-shutdown" not in args


# Checks POSIX termination lets short work finish and cancels longer work without capacity leaks.


# Uses a supplied service's settings consistently across schema, middleware, and logging.
def test_service_only_application_uses_service_settings(policy_service):
    from fastapi.testclient import TestClient
    from model_bridge.api.dependencies import (
        AuthenticatedTenant,
        get_authenticated_tenant,
    )

    service = policy_service(
        platform={
            "max_output_tokens": 16384,
            "max_message_characters": 8,
            "max_input_bytes": 512,
        },
        tenant={"max_output_tokens": 16384, "max_input_bytes": 512},
    )
    app = main.create_app(service=service)
    app.dependency_overrides[get_authenticated_tenant] = lambda: AuthenticatedTenant(
        id="test-tenant"
    )
    payload = {
        "request_id": "service-config",
        "message": "hello",
        "model_preference": "fast",
        "max_tokens": 10000,
    }
    with TestClient(app) as client:
        assert client.post("/chat", json=payload).status_code == 200
        assert (
            client.post("/chat", json={**payload, "message": "123456789"}).status_code
            == 422
        )
        body = json.dumps({**payload, "request_id": "oversized"}).encode()
        body += b" " * (513 - len(body))
        assert (
            client.post(
                "/chat", content=body, headers={"Content-Type": "application/json"}
            ).status_code
            == 413
        )
    assert service.generate.await_count == 1


# Rejects explicit conflicting configurations instead of silently mixing runtime policies.
def test_application_rejects_service_settings_mismatch(policy_service):
    from model_bridge.config.models import Settings

    service = policy_service()
    data = service.settings.model_dump()
    data["platform"]["max_output_tokens"] += 1
    with pytest.raises(ValueError, match="settings must match"):
        main.create_app(Settings.model_validate(data), service=service)
