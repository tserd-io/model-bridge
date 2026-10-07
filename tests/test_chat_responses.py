"""Application outcomes retain the API's response and header semantics."""

import json
import pytest
from model_bridge.api.responses import to_http_response
from model_bridge.application.outcomes import ChatOutcome, ChatResult


# Verifies HTTP policy for every outcome after moving conversion out of the service.
@pytest.mark.parametrize(
    "kind, status, code",
    [
        ("success", "success", 200),
        ("in_progress", "in_progress", 202),
        ("unknown", "unknown", 202),
        ("conflict", "failed", 409),
        ("rate_limited", "failed", 429),
        ("provider_rejected", "failed", 502),
        ("failed", "failed", 503),
        ("invalid_request", "failed", 422),
        ("input_too_large", "failed", 413),
    ],
)
def test_outcome_http_status_and_body(kind, status, code):
    result = ChatResult(
        request_id="response-check",
        tenant_id="test-tenant",
        status=status,
        detail="Outcome detail",
        attempts=2,
    )
    response = to_http_response(ChatOutcome(kind=kind, response=result))
    assert response.status_code == code
    body = json.loads(response.body)
    assert body["status"] == status
    assert body["tenant_id"] == "test-tenant"
    assert body["detail"] == "Outcome detail"
    assert body["attempts"] == 2
    assert "Retry-After" not in response.headers
    assert "X-Idempotent-Replay" not in response.headers


# Verifies retry guidance and replay markers survive conversion without mutating the result.
def test_outcome_delivery_headers():
    pending = to_http_response(
        ChatOutcome(
            kind="in_progress",
            response=ChatResult("pending", "in_progress"),
            retry_after=1,
        )
    )
    limited = to_http_response(
        ChatOutcome(
            kind="rate_limited",
            response=ChatResult("limited", "failed"),
            retry_after=7,
        )
    )
    replay_result = ChatResult("replayed", "success", content="saved", cache_hit=True)
    replay = to_http_response(
        ChatOutcome(
            kind="success",
            response=replay_result,
            replayed=True,
        )
    )
    assert pending.headers["Retry-After"] == "1"
    assert limited.headers["Retry-After"] == "7"
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert json.loads(replay.body)["cache_hit"] is True
    assert replay_result.content == "saved"
