"""Input limits must reject oversized work before storage or generation."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from scripts import main
from scripts.contracts import GenerationResult
from scripts.load_settings import PLATFORM_SETTINGS
from scripts.rate_limit import SlidingWindowRateLimiter
from scripts.request_store import RequestStore


# Gives input tests temporary storage and generation that never contacts a provider.
@pytest.fixture
def input_app(monkeypatch, tmp_path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    claim = Mock(wraps=store.claim)
    monkeypatch.setattr(store, "claim", claim)
    monkeypatch.setattr(main, "request_store", store)
    monkeypatch.setattr(
        main, "chat_rate_limiter", SlidingWindowRateLimiter(100, 60)
    )
    generation = AsyncMock(return_value=GenerationResult(
        content="ok", model="fake-fast", provider="fake", attempts=1,
    ))
    monkeypatch.setattr(main, "llm_call", generation)
    return main.app, generation, claim


# Supplies a valid chat payload while allowing each test to control message size.
def payload(message="hello"):
    return {
        "request_id": "input-limit-test",
        "message": message,
        "model_preference": "fast",
        "max_tokens": 10,
    }


# Checks that the exact character boundary is accepted, including multibyte text.
@pytest.mark.parametrize("character", ["a", "é"])
def test_message_at_character_limit_is_accepted(input_app, character):
    app, generation, claim = input_app
    limit = getattr(PLATFORM_SETTINGS, "max_message_characters", 32768)
    with TestClient(app) as client:
        response = client.post("/chat", json=payload(character * limit))
    assert response.status_code == 200
    generation.assert_awaited_once()
    claim.assert_called_once()


# Checks that one extra character produces validation failure without admitting work.
@pytest.mark.parametrize("character", ["a", "é"])
def test_message_above_character_limit_is_rejected(input_app, character):
    app, generation, claim = input_app
    limit = getattr(PLATFORM_SETTINGS, "max_message_characters", 32768)
    with TestClient(app) as client:
        response = client.post("/chat", json=payload(character * (limit + 1)))
    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "message"]
        and error["type"] == "string_too_long"
        for error in response.json()["detail"]
    )
    generation.assert_not_awaited()
    claim.assert_not_called()


# Delivers raw ASGI chunks so tests can control headers independently of body bytes.
async def send_body(app, chunks, headers):
    messages = [
        {"type": "http.request", "body": chunk,
         "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    sent = []

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/chat", "raw_path": b"/chat", "root_path": "",
        "query_string": b"", "headers": headers,
        "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
    }
    await app(scope, receive, send)
    starts = [item for item in sent if item["type"] == "http.response.start"]
    assert len(starts) == 1
    return starts[0]["status"]


# Builds a valid JSON body whose total byte size includes harmless trailing whitespace.
def body_with_size(size):
    body = json.dumps(payload()).encode("utf-8")
    assert size >= len(body)
    return body + b" " * (size - len(body))


# Checks the exact byte boundary is accepted without a Content-Length header.
def test_body_at_byte_limit_is_accepted(input_app):
    app, generation, claim = input_app
    body = body_with_size(PLATFORM_SETTINGS.max_input_bytes)
    status = asyncio.run(send_body(
        app, [body], [(b"content-type", b"application/json")],
    ))
    assert status == 200
    generation.assert_awaited_once()
    claim.assert_called_once()


# Checks actual received bytes defeat absent, accurate, or understated length headers.
@pytest.mark.parametrize("length_header", ["absent", "accurate", "understated"])
def test_body_above_byte_limit_is_rejected(input_app, length_header):
    app, generation, claim = input_app
    limit = PLATFORM_SETTINGS.max_input_bytes
    body = body_with_size(limit + 1)
    headers = [(b"content-type", b"application/json")]
    if length_header != "absent":
        length = len(body) if length_header == "accurate" else 1
        headers.append((b"content-length", str(length).encode("ascii")))
    status = asyncio.run(send_body(app, [body[:limit], body[limit:]], headers))
    assert status == 413
    generation.assert_not_awaited()
    claim.assert_not_called()


# Checks oversized malformed JSON is rejected by byte admission before JSON parsing.
def test_oversized_invalid_json_returns_413(input_app):
    app, generation, claim = input_app
    body = b"x" * (PLATFORM_SETTINGS.max_input_bytes + 1)
    status = asyncio.run(send_body(
        app, [body], [(b"content-type", b"application/json")],
    ))
    assert status == 413
    generation.assert_not_awaited()
    claim.assert_not_called()
