"""Check the real Uvicorn server using isolated storage and a fake provider."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


# Selects an available loopback port for the short-lived server.
def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


# Sends a bounded local request without using environment-configured proxies.
def request_json(opener, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=2) as response:
        if response.status != 200:
            raise AssertionError(f"Unexpected HTTP status: {response.status}")
        return json.load(response)


# Starts Uvicorn, verifies HTTP behavior, and always releases the child process.
def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    port = available_port()
    base_url = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    with TemporaryDirectory(prefix="model-bridge-startup-") as directory:
        temporary_root = Path(directory)
        log_path = temporary_root / "uvicorn.log"
        environment = {
            **os.environ,
            "LLM_PROVIDER": "fake",
            "LLM_FALLBACK_PROVIDER": "",
            "OPENAI_API_KEY": "",
            "IDEMPOTENCY_DB_PATH": str(temporary_root / "requests.sqlite3"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "scripts.main:app",
                    "--host", "127.0.0.1", "--port", str(port),
                    "--workers", "1", "--no-access-log",
                ],
                cwd=project_root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Uvicorn exited with code {process.returncode}")
                    try:
                        live = request_json(opener, f"{base_url}/health/live")
                        break
                    except (URLError, TimeoutError):
                        # Connection attempts can fail while the application imports.
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Uvicorn did not start within 20 seconds")
                        time.sleep(0.1)

                if live != {"status": "alive"}:
                    raise AssertionError(f"Unexpected liveness response: {live}")
                ready = request_json(opener, f"{base_url}/health/ready")
                if ready != {"status": "ready", "checks": {"database": "ok"}}:
                    raise AssertionError(f"Unexpected readiness response: {ready}")
                result = request_json(opener, f"{base_url}/chat", {
                    "request_id": "startup-check",
                    "message": "startup probe",
                    "model_preference": "fast",
                    "max_tokens": 16,
                })
                expected = {
                    "request_id": "startup-check",
                    "status": "success",
                    "provider": "fake",
                    "model": "fake-fast",
                    "content": "Fake response: startup probe",
                    "attempts": 1,
                }
                if any(result.get(key) != value for key, value in expected.items()):
                    raise AssertionError(f"Unexpected chat response: {result}")
                if process.poll() is not None:
                    raise RuntimeError("Uvicorn exited during the startup check")
            except Exception:
                # Surface child-process diagnostics in the failed CI step.
                log.flush()
                print(log_path.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
                raise
            finally:
                # Stop the server before temporary logs and SQLite files are removed.
                if process.poll() is None:
                    if os.name == "nt":
                        # Windows virtualenv launchers can own a second Python process.
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            check=True, capture_output=True, timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    print("Uvicorn startup check passed: liveness, readiness, and fake-provider chat.")


if __name__ == "__main__":
    main()
