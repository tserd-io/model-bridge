"""Linux-only process shutdown contracts."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import pytest
from model_bridge import main

pytestmark = pytest.mark.e2e


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
    root = Path(__file__).resolve().parents[2]
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
