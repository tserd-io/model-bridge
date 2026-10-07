"""Configuration contracts for application logs and supported server startup.

The proposed startup interface is main.run_server(settings=..., host=..., port=...).
Its implementation is deliberately left to the next application-code change.
"""

import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
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
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8").replace("\\\n", "")
    command = re.search(r"^CMD\s+(\[.*\])\s*$", text, re.MULTILINE | re.DOTALL)
    assert command is not None, "Use an explicit JSON Docker CMD"
    args = json.loads(command.group(1))
    assert args[:3] == ["python", "-m", "model_bridge.main"]
    assert "--timeout-graceful-shutdown" not in args


# Checks POSIX termination lets short work finish and cancels longer work without capacity leaks.
@pytest.mark.skipif(
    sys.platform != "linux", reason="Real SIGTERM shutdown check requires Linux"
)
@pytest.mark.allow_hosts(["127.0.0.1"])
@pytest.mark.parametrize("finishes", [True, False])
def test_active_request_during_server_shutdown(tmp_path, finishes):
    assert callable(getattr(main, "run_server", None)), (
        "Implement configured server startup first"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    child = tmp_path / "shutdown_probe.py"
    child.write_text(
        """
import asyncio, json, os
from pathlib import Path
from types import SimpleNamespace
from model_bridge import main
from model_bridge.config.loader import SETTINGS
from model_bridge.config.models import Settings
from model_bridge.api.dependencies import AuthenticatedTenant, get_authenticated_tenant
from model_bridge.providers.contracts import GenerationResult
directory = Path(os.environ["PROBE_DIRECTORY"])
data = SETTINGS.model_dump()
data["platform"]["shutdown_grace_seconds"] = 2
settings = Settings.model_validate(data)
service = main.create_chat_service(settings)
async def complete(**kwargs):
    (directory / "entered").touch()
    try:
        while not (directory / "release").exists():
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        (directory / "cancelled").touch()
        raise
    return GenerationResult("completed", "fake-fast")
service.primary_provider = SimpleNamespace(complete=complete)
app = main.create_app(settings=settings, service=service)
async def identity(): return AuthenticatedTenant(id="test-tenant")
app.dependency_overrides[get_authenticated_tenant] = identity
@app.on_event("shutdown")
async def stopped():
    (directory / "state.json").write_text(json.dumps({
        "active": service.generation_limiter._active,
        "tenants": service.generation_limiter._tenant_active,
    }))
main.create_app = lambda **kwargs: app
main.run_server(settings=settings, host="127.0.0.1", port=int(os.environ["PROBE_PORT"]))
""",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(root),
        "LLM_PROVIDER": "fake",
        "LLM_FALLBACK_PROVIDER": "",
        "OPENAI_API_KEY": "",
        "IDEMPOTENCY_DB_PATH": str(tmp_path / "shutdown.sqlite3"),
        "PROBE_DIRECTORY": str(tmp_path),
        "PROBE_PORT": str(port),
    }
    opener = build_opener(ProxyHandler({}))
    url = f"http://127.0.0.1:{port}"

    # Sends one bounded local request from a separate thread while the server shuts down.
    def request():
        payload = json.dumps(
            {
                "request_id": "shutdown",
                "message": "hello",
                "model_preference": "fast",
                "max_tokens": 10,
            }
        ).encode()
        with opener.open(
            Request(
                url + "/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
            ),
            timeout=8,
        ) as response:
            return response.status

    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(child)],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, (tmp_path / "server.log").read_text()
                try:
                    with opener.open(url + "/health/live", timeout=0.5):
                        break
                except (URLError, TimeoutError):
                    assert time.monotonic() < deadline, "Server did not start"
                    time.sleep(0.02)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(request)
                deadline = time.monotonic() + 3
                while not (tmp_path / "entered").exists():
                    assert time.monotonic() < deadline, "Provider was not entered"
                    time.sleep(0.01)
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                if finishes:
                    time.sleep(0.2)
                    (tmp_path / "release").touch()
                    assert future.result(timeout=6) == 200
                else:
                    with pytest.raises(HTTPError) as error:
                        future.result(timeout=6)
                    assert error.value.code == 500
                process.wait(timeout=5)
                assert time.monotonic() - started < 5
            # Uvicorn may re-raise SIGTERM after orderly cleanup.
            assert process.returncode in (0, -signal.SIGTERM)
            assert (tmp_path / "cancelled").exists() is (not finishes)
            state = json.loads((tmp_path / "state.json").read_text())
            assert state == {"active": 0, "tenants": {}}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


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
