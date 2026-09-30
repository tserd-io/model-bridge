import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import scripts.functions as functions
import scripts.main as main_module
from scripts.config import MAX_LLM_ATTEMPTS
from scripts.evaluations.run_eval import run_evaluation
from scripts.functions import llm_call
from scripts.main import app
from scripts.providers import (
    FakeProvider,
    GenerationResult,
    RetryableProviderError,
)
from scripts.request_store import RequestStore


class RecordingFakeProvider(FakeProvider):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        self.calls.append((request_id, attempt, message))
        return await super().complete(
            request_id,
            attempt,
            message,
            model_preference,
            max_tokens,
            timeout,
        )


class FailTwiceFakeProvider(RecordingFakeProvider):
    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        self.calls.append((request_id, attempt, message))
        if attempt < 3:
            raise RetryableProviderError("simulated 429")
        return GenerationResult(
            content="recovered",
            model=f"fake-{model_preference}",
        )


class UnknownOutcomeFakeProvider:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        self.call_count += 1
        raise RetryableProviderError(
            "simulated timeout",
            outcome_unknown=True,
        )


class AlwaysRetryFakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        self.calls.append((request_id, attempt))
        raise RetryableProviderError("simulated rate limit")


class FallbackFakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        self.calls.append((request_id, attempt))
        return GenerationResult(
            content="fallback answer",
            model="fallback-model",
            provider="fallback-fake",
        )


def request_payload(request_id: str, message: str = "hello") -> dict[str, object]:
    return {
        "request_id": request_id,
        "message": message,
        "model_preference": "fast",
        "max_tokens": 50,
    }


@pytest.fixture
def isolated_request_store(monkeypatch: pytest.MonkeyPatch):
    database_path = Path.cwd() / f".pytest-requests-{uuid4().hex}.sqlite3"
    monkeypatch.setattr(
        main_module,
        "request_store",
        RequestStore(database_path),
    )
    try:
        yield
    finally:
        for suffix in ("", "-wal", "-shm"):
            database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)


def test_post_response_is_normalized_and_duplicate_replays_saved_response(
    monkeypatch: pytest.MonkeyPatch,
    isolated_request_store,
) -> None:
    provider = RecordingFakeProvider()
    monkeypatch.setattr(functions, "provider", provider)
    payload = request_payload(f"pytest-{uuid4().hex}")

    with TestClient(app) as client:
        first = client.post("/chat", json=payload)
        duplicate = client.post("/chat", json=payload)
        metrics = client.get(
            "/metrics",
            headers={
                "Accept": "application/openmetrics-text; version=1.0.0; charset=utf-8"
            },
        )

    body = first.json()
    assert first.status_code == 200
    assert body["request_id"] == payload["request_id"]
    assert body["status"] == "success"
    assert body["model"] == "fake-fast"
    assert body["provider"] == "fake"
    assert body["content"] == "Fake response: hello"
    assert body["attempts"] == 1
    assert duplicate.json() == body
    assert len(provider.calls) == 1
    assert 'request_id="' + payload["request_id"] + '"' in metrics.text


def test_reusing_request_id_with_different_payload_returns_conflict(
    monkeypatch: pytest.MonkeyPatch,
    isolated_request_store,
) -> None:
    provider = RecordingFakeProvider()
    monkeypatch.setattr(functions, "provider", provider)
    payload = request_payload(f"pytest-{uuid4().hex}")

    with TestClient(app) as client:
        original = client.post("/chat", json=payload)
        conflict = client.post(
            "/chat",
            json={**payload, "message": "different message"},
        )

    assert original.status_code == 200
    assert conflict.status_code == 409
    assert len(provider.calls) == 1


def test_retry_attempts_reuse_request_id_and_report_attempt_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FailTwiceFakeProvider()
    monkeypatch.setattr(functions, "provider", provider)
    request_id = f"pytest-retry-{uuid4().hex}"

    result = asyncio.run(
        llm_call(
            request_id=request_id,
            message="retry me",
            model_preference="fast",
            max_tokens=50,
            request_deadline_seconds=5,
            max_attempts=3,
            retry_base_delay_seconds=0,
        )
    )

    assert result.content == "recovered"
    assert result.attempts == 3
    assert [call[0] for call in provider.calls] == [request_id] * 3
    assert [call[1] for call in provider.calls] == [1, 2, 3]


def test_configured_fallback_uses_remaining_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PrimaryProvider:
        async def complete(
            self,
            request_id: str,
            attempt: int,
            message: str,
            model_preference: str,
            max_tokens: int,
            timeout: float,
        ) -> GenerationResult:
            raise RetryableProviderError("simulated 429")

    class FallbackProvider:
        async def complete(
            self,
            request_id: str,
            attempt: int,
            message: str,
            model_preference: str,
            max_tokens: int,
            timeout: float,
        ) -> GenerationResult:
            assert request_id == expected_request_id
            assert attempt == 2
            return GenerationResult(
                content="fallback worked",
                model="fallback-model",
                provider="fallback-provider",
            )

    expected_request_id = f"pytest-fallback-{uuid4().hex}"
    monkeypatch.setattr(functions, "provider", PrimaryProvider())
    monkeypatch.setattr(functions, "fallback_provider", FallbackProvider())
    monkeypatch.setattr(functions, "FALLBACK_PROVIDER", "fallback-provider")

    result = asyncio.run(
        llm_call(
            request_id=expected_request_id,
            message="try fallback",
            model_preference="fast",
            max_tokens=50,
            max_attempts=3,
            retry_base_delay_seconds=0,
        )
    )

    assert result.provider == "fallback-provider"
    assert result.model == "fallback-model"
    assert result.attempts == 2


def test_retry_uses_configured_fallback_and_reports_actual_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = AlwaysRetryFakeProvider()
    fallback = FallbackFakeProvider()
    monkeypatch.setattr(functions, "provider", primary)
    monkeypatch.setattr(functions, "fallback_provider", fallback)
    monkeypatch.setattr(functions, "FALLBACK_PROVIDER", "fallback-fake")
    request_id = f"pytest-fallback-{uuid4().hex}"

    result = asyncio.run(
        llm_call(
            request_id=request_id,
            message="fall back",
            model_preference="fast",
            max_tokens=50,
            request_deadline_seconds=5,
            max_attempts=3,
            retry_base_delay_seconds=0,
        )
    )

    assert len(primary.calls) == 1
    assert fallback.calls == [(request_id, 2)]
    assert result.provider == "fallback-fake"
    assert result.attempts == 2


def test_unknown_provider_outcome_is_persisted_and_not_called_again(
    monkeypatch: pytest.MonkeyPatch,
    isolated_request_store,
) -> None:
    provider = UnknownOutcomeFakeProvider()
    monkeypatch.setattr(functions, "provider", provider)
    payload = request_payload(f"pytest-unknown-{uuid4().hex}")

    with TestClient(app) as client:
        first = client.post("/chat", json=payload)
        duplicate = client.post("/chat", json=payload)

    assert first.status_code == 202
    assert first.json()["status"] == "unknown"
    assert first.json()["attempts"] == MAX_LLM_ATTEMPTS
    assert first.json()["provider"] is None
    assert duplicate.json() == first.json()
    assert provider.call_count == MAX_LLM_ATTEMPTS


@pytest.mark.parametrize(
    ("task_type", "requested_preference", "expected_model", "human_review"),
    [
        ("simple", "balanced", "fake-fast", False),
        ("complex", "fast", "fake-balanced", False),
        ("high_risk", "fast", "fake-balanced", True),
        (None, "fast", "fake-fast", False),
    ],
)
def test_task_routing_uses_requirements_and_flags_high_risk(
    monkeypatch: pytest.MonkeyPatch,
    isolated_request_store,
    task_type: str | None,
    requested_preference: str,
    expected_model: str,
    human_review: bool,
) -> None:
    monkeypatch.setattr(functions, "provider", FakeProvider())
    payload = request_payload(f"pytest-route-{uuid4().hex}")
    payload["model_preference"] = requested_preference
    if task_type is not None:
        payload["task_type"] = task_type

    response = TestClient(app).post("/chat", json=payload)

    assert response.status_code == 200
    assert response.json()["model"] == expected_model
    assert response.json()["human_review_required"] is human_review


def test_evaluation_runner_persists_versions_labels_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(functions, "provider", FakeProvider())
    dataset = {
        "dataset_version": "unit-v1",
        "prompt_version": "prompt-v7",
        "cases": [
            {
                "case_id": "echo-orange",
                "task_type": "simple",
                "message": "say orange",
                "model_preference": "balanced",
                "max_tokens": 32,
                "expected": {
                    "allowed_answers": [],
                    "answer_points": ["orange"],
                    "expected_fields": None,
                    "expected_sources": [],
                },
                "allowed_answer_variation": "answer may include other wording",
                "known_failure_category": "instruction_following",
            }
        ],
    }

    with TemporaryDirectory() as directory:
        root = Path(directory)
        dataset_path = root / "dataset.json"
        dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
        summary = run_evaluation(dataset_path, root / "results")
        result_path = next((root / "results").glob("*.jsonl"))
        result = json.loads(result_path.read_text(encoding="utf-8").splitlines()[0])

    assert summary["dataset_version"] == "unit-v1"
    assert summary["prompt_version"] == "prompt-v7"
    assert summary["pass_rate"] == 1.0
    assert summary["estimated_cost_usd"] is None
    assert result["passed"] is True
    assert result["model_version"] == "fake-fast"
    assert result["known_failure_category"] == "instruction_following"
    assert result["grounding"]["faithfulness"] is None
    assert result["agentic_metrics"]["applicable"] is False
