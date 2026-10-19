"""Compatibility command for acceptance tests, with an explicit live-provider opt-in."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


# Runs the same deterministic scenarios as CI instead of maintaining duplicate assertions.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also send one request to the configured primary provider",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/acceptance",
            "tests/unit/test_provider_adapters.py",
            "-q",
        ],
        cwd=root,
        check=True,
    )
    if not args.live:
        return

    # A live opt-in uses disposable state and keeps identity simulation inside tests.
    with TemporaryDirectory(prefix="model-bridge-live-") as directory:
        os.environ["IDEMPOTENCY_DB_PATH"] = str(Path(directory) / "live.sqlite3")
        os.environ["LLM_FALLBACK_PROVIDER"] = ""
        from fastapi.testclient import TestClient
        from model_bridge.main import create_app
        from model_bridge.api.dependencies import (
            AuthenticatedTenant,
            get_authenticated_tenant,
        )

        app = create_app()
        app.dependency_overrides[get_authenticated_tenant] = lambda: (
            AuthenticatedTenant("test-tenant")
        )
        with TestClient(app) as client:
            response = client.post(
                "/chat",
                json={
                    "request_id": "live-probe",
                    "message": "Reply with: Model Bridge is working.",
                    "model_preference": "fast",
                    "max_tokens": 50,
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["content"]
        print(f"Live provider check passed: {response.json()['provider']}")


if __name__ == "__main__":
    main()
