"""Input limits must reject oversized work before storage or generation."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from model_bridge import main
from model_bridge.providers.contracts import GenerationResult
from model_bridge.config.loader import PLATFORM_SETTINGS
from model_bridge.execution.rate_limit import TenantRateLimiter
from model_bridge.storage.request_store import RequestStore
from model_bridge.application.outcomes import ChatCommand
from model_bridge.api.dependencies import AuthenticatedTenant, get_authenticated_tenant


# Gives input tests temporary storage and generation that never contacts a provider.
@pytest.fixture
def input_app(monkeypatch, tmp_path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    claim = Mock(wraps=store.claim)
    monkeypatch.setattr(store, "claim", claim)
    monkeypatch.setattr(main.app.state.chat_service, "request_store", store)
    monkeypatch.setattr(
        main.app.state.chat_service,
        "chat_rate_limiter",
        TenantRateLimiter(100, 60),
    )
    generation = AsyncMock(
        return_value=GenerationResult(
            content="ok",
            model="fake-fast",
            provider="fake",
            attempts=1,
        )
    )
    monkeypatch.setattr(main.app.state.chat_service, "generate", generation)
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
        error["loc"] == ["body", "message"] and error["type"] == "string_too_long"
        for error in response.json()["detail"]
    )
    generation.assert_not_awaited()
    claim.assert_not_called()


# Delivers raw ASGI chunks so tests can control headers independently of body bytes.
async def send_body(app, chunks, headers):
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
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
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat",
        "raw_path": b"/chat",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
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
    status = asyncio.run(
        send_body(
            app,
            [body],
            [(b"content-type", b"application/json")],
        )
    )
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
    status = asyncio.run(
        send_body(
            app,
            [body],
            [(b"content-type", b"application/json")],
        )
    )
    assert status == 413
    generation.assert_not_awaited()
    claim.assert_not_called()


# Calculates the agreed caller-input representation, excluding trusted tenant identity.
def normalized_bytes(data):
    fields = {
        name: data.get(name)
        for name in (
            "request_id",
            "message",
            "model_preference",
            "max_tokens",
            "task_type",
        )
    }
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# Checks the tenant's token boundary through HTTP and direct service calls before claims.
@pytest.mark.parametrize("tokens", [2047, 2048, 2049])
@pytest.mark.parametrize("entry_point", ["http", "service"])
def test_tenant_token_boundary(policy_service, tokens, entry_point):
    service = policy_service(tenant={"max_output_tokens": 2048})
    claim = Mock(wraps=service.request_store.claim)
    service.request_store.claim = claim
    data = {**payload(), "max_tokens": tokens}
    if entry_point == "http":
        with TestClient(main.app) as client:
            response = client.post("/chat", json=data)
        accepted = response.status_code == 200
        if tokens > 2048:
            assert response.status_code == 422
    else:
        command = ChatCommand(tenant_id="test-tenant", **data)
        outcome = asyncio.run(service.handle(command))
        accepted = outcome.kind == "success"
        if tokens > 2048:
            assert outcome.kind == "invalid_request"
    assert accepted is (tokens <= 2048)
    if accepted:
        assert service.generate.await_args.kwargs["max_tokens"] == tokens
        claim.assert_called_once()
    else:
        service.generate.assert_not_awaited()
        claim.assert_not_called()


# Checks actual tenant body bytes across encodings, delivery styles, and length headers.
@pytest.mark.parametrize("message", ["hello", "é", '\n"\\'])
@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("length_header", ["absent", "accurate", "understated"])
@pytest.mark.parametrize("extra", [0, 1])
def test_tenant_body_byte_boundary(
    policy_service, message, chunked, length_header, extra
):
    service = policy_service(tenant={"max_input_bytes": 512})
    claim = Mock(wraps=service.request_store.claim)
    service.request_store.claim = claim
    body = json.dumps(payload(message), ensure_ascii=False).encode("utf-8")
    body += b" " * (512 + extra - len(body))
    headers = [(b"content-type", b"application/json")]
    if length_header != "absent":
        length = len(body) if length_header == "accurate" else 1
        headers.append((b"content-length", str(length).encode("ascii")))
    chunks = [body[:256], body[256:]] if chunked else [body]
    status = asyncio.run(send_body(main.app, chunks, headers))
    assert status == (200 if extra == 0 else 413)
    if extra:
        claim.assert_not_called()
        service.generate.assert_not_awaited()
    else:
        service.generate.assert_awaited_once()


# Checks the shared normalized-size boundary, including multibyte caller input.
@pytest.mark.parametrize("entry_point", ["http", "service"])
@pytest.mark.parametrize("extra", [0, 1])
def test_normalized_input_boundary(policy_service, entry_point, extra):
    data = payload("é" * 100)
    limit = len(normalized_bytes(data)) - extra
    service = policy_service(tenant={"max_input_bytes": limit})
    claim = Mock(wraps=service.request_store.claim)
    service.request_store.claim = claim
    if entry_point == "http":
        # Omit task_type so the transport body is smaller than normalized input.
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        assert len(body) <= limit
        with TestClient(main.app) as client:
            result = client.post(
                "/chat", content=body, headers={"Content-Type": "application/json"}
            )
        assert result.status_code == (200 if extra == 0 else 413)
    else:
        result = asyncio.run(
            service.handle(ChatCommand(tenant_id="test-tenant", **data))
        )
        assert result.kind == ("success" if extra == 0 else "input_too_large")
    if extra:
        claim.assert_not_called()
        service.generate.assert_not_awaited()
    else:
        service.generate.assert_awaited_once()


# Checks tenant-specific body allowances without changing the trusted identity boundary.
def test_tenant_body_allowances_are_independent(policy_service, monkeypatch):
    service = policy_service(
        tenant={"max_input_bytes": 512},
        overrides={"tenant-b": {"max_input_bytes": 1024}},
    )
    current = ["tenant-a"]

    # Supplies controlled test identities; no client header becomes production authentication.
    async def identity():
        return AuthenticatedTenant(id=current[0])

    monkeypatch.setitem(
        main.app.dependency_overrides, get_authenticated_tenant, identity
    )
    body = body_with_size(768)
    with TestClient(main.app) as client:
        rejected = client.post(
            "/chat", content=body, headers={"Content-Type": "application/json"}
        )
        current[0] = "tenant-b"
        accepted = client.post(
            "/chat", content=body, headers={"Content-Type": "application/json"}
        )
    assert rejected.status_code == 413
    assert accepted.status_code == 200
    service.generate.assert_awaited_once()


# Checks transport formatting and trusted identity length do not change normalized size.
def test_input_formatting_replays_without_size_metadata_in_hash(
    policy_service, monkeypatch
):
    service = policy_service(tenant={"max_input_bytes": 512})
    with TestClient(main.app) as client:
        first = client.post("/chat", json=payload())
        body = json.dumps(payload(), indent=2).encode() + b" " * 20
        replay = client.post(
            "/chat", content=body, headers={"Content-Type": "application/json"}
        )
    assert first.status_code == replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    service.generate.assert_awaited_once()


# Checks a long trusted tenant identifier is excluded from the direct-input byte budget.
@pytest.mark.parametrize("tenant_id", ["a", "tenant-" + "a" * 50])
def test_trusted_identity_is_excluded_from_input_size(policy_service, tenant_id):
    data = payload("é" * 100)
    service = policy_service(tenant={"max_input_bytes": len(normalized_bytes(data))})
    outcome = asyncio.run(service.handle(ChatCommand(tenant_id=tenant_id, **data)))
    assert outcome.kind == "success"


# Checks every caller-controlled field contributes to the normalized direct-service budget.
@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "input-limit-test-longer"),
        ("message", "helloé"),
        ("model_preference", "balanced"),
        ("max_tokens", 100),
        ("task_type", "complex"),
    ],
)
def test_all_caller_fields_count_toward_normalized_size(policy_service, field, value):
    data = payload()
    limit = len(normalized_bytes(data))
    larger = {**data, field: value}
    assert len(normalized_bytes(larger)) > limit
    service = policy_service(tenant={"max_input_bytes": limit})
    claim = Mock(wraps=service.request_store.claim)
    service.request_store.claim = claim
    result = asyncio.run(service.handle(ChatCommand(tenant_id="test-tenant", **larger)))
    assert result.kind == "input_too_large"
    claim.assert_not_called()
    service.generate.assert_not_awaited()


# Checks omitted and explicit null task types produce the same request identity.
def test_missing_and_null_task_type_replay_identically(policy_service):
    service = policy_service(tenant={"max_input_bytes": 512})
    with TestClient(main.app) as client:
        first = client.post("/chat", json=payload())
        replay = client.post("/chat", json={**payload(), "task_type": None})
    assert first.status_code == replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    service.generate.assert_awaited_once()


# Confirms app-specific schema limits can exceed the former hardcoded token ceiling.
def test_custom_application_token_ceiling(policy_service):
    from model_bridge import main
    from model_bridge.api.dependencies import (
        get_authenticated_tenant,
        AuthenticatedTenant,
    )
    from fastapi.testclient import TestClient

    service = policy_service(
        platform={"max_output_tokens": 16384}, tenant={"max_output_tokens": 16384}
    )
    application = main.create_app(service.settings, service=service)
    application.dependency_overrides[get_authenticated_tenant] = lambda: (
        AuthenticatedTenant(id="test-tenant")
    )
    with TestClient(application) as client:
        response = client.post(
            "/chat",
            json={
                "request_id": "custom-ceiling",
                "message": "hello",
                "model_preference": "fast",
                "max_tokens": 10000,
            },
        )
    assert response.status_code == 200
    assert service.generate.await_args.kwargs["max_tokens"] == 10000


# Confirms custom message limits preserve FastAPI field validation and avoid provider work.
def test_custom_application_message_ceiling(policy_service):
    from model_bridge import main
    from model_bridge.api.dependencies import (
        get_authenticated_tenant,
        AuthenticatedTenant,
    )
    from fastapi.testclient import TestClient

    service = policy_service(platform={"max_message_characters": 8})
    application = main.create_app(service.settings, service=service)
    application.dependency_overrides[get_authenticated_tenant] = lambda: (
        AuthenticatedTenant(id="test-tenant")
    )
    with TestClient(application) as client:
        response = client.post(
            "/chat",
            json={
                "request_id": "message-ceiling",
                "message": "123456789",
                "model_preference": "fast",
                "max_tokens": 1,
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "message"]
    service.generate.assert_not_awaited()


# Rejects unpaired Unicode surrogates safely instead of failing during UTF-8 size calculation.
def test_invalid_utf8_direct_input(policy_service):
    import asyncio
    from model_bridge.application.outcomes import ChatCommand

    service = policy_service()
    command = ChatCommand("unicode-check", "test-tenant", chr(0xD800), "fast", 1)
    result = asyncio.run(service.handle(command))
    assert result.kind == "invalid_request"
    service.generate.assert_not_awaited()


# Invalid Unicode returns a serializable field error without reaching storage, logs, or the provider.
@pytest.mark.parametrize(
    "field, repeat",
    [("request_id", 1), ("message", 1), ("tenant_id", 1), ("message", 32769)],
)
def test_invalid_utf8_http_input_is_safely_rejected(policy_service, field, repeat):
    import json
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    from model_bridge import main
    from model_bridge.api.dependencies import (
        AuthenticatedTenant,
        get_authenticated_tenant,
    )

    service = policy_service()
    claim = Mock(side_effect=AssertionError("Invalid input must not claim storage"))
    service.request_store.claim = claim
    app = main.create_app(service=service)
    app.dependency_overrides[get_authenticated_tenant] = lambda: AuthenticatedTenant(
        id="test-tenant"
    )
    payload = {
        "request_id": "unicode-http",
        "message": "hello",
        "model_preference": "fast",
        "max_tokens": 1,
    }
    payload[field] = chr(0xD800) * repeat
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/chat",
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422
    details = response.json()["detail"]
    assert details[0]["loc"] == ["body", field]
    assert all("input" not in issue for issue in details)
    assert chr(0xD800) not in response.text
    claim.assert_not_called()
    service.generate.assert_not_awaited()
