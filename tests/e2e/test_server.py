"""The existing bounded startup probe, collected by pytest like every other check."""

import pytest
from tests.check_startup import main as check_startup

pytestmark = [pytest.mark.e2e, pytest.mark.allow_hosts(["127.0.0.1"])]


# Starts a real Uvicorn process and verifies health and chat over loopback HTTP.
def test_server_startup_and_chat():
    check_startup()
