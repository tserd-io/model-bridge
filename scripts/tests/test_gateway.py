import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import scripts.rate_limit as rate_limit_module
from scripts.rate_limit import SlidingWindowRateLimiter
import scripts.main as main_module
from scripts.load_settings import MAX_LLM_ATTEMPTS
from scripts.evaluations.eval_runner import run_evaluation
from scripts.generation import llm_call
from scripts.main import app
from scripts.contracts import GenerationResult, RetryableProviderError
from scripts.providers import FakeProvider
from scripts.circuit_breaker import CircuitBreaker
from scripts.request_store import RequestStore
# Gives API tests fake providers and prevents configured fallback calls.
@pytest.fixture(autouse=True)
def isolated_api_providers(monkeypatch):
    monkeypatch.setattr(main_module, "primary_provider", FakeProvider())
    monkeypatch.setattr(main_module, "backup_provider", None)
    monkeypatch.setattr(main_module, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(main_module, "FALLBACK_PROVIDER", None)
    monkeypatch.setattr(main_module, "primary_breaker", CircuitBreaker())
    monkeypatch.setattr(main_module, "backup_breaker", None)
# Replaces only the limiter clock so tests can advance time without waiting.
@pytest.fixture
def limiter_clock(monkeypatch):
    now = [100.0]

    monkeypatch.setattr(
        rate_limit_module,
        "time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    return now
# Gives each test a fresh limiter so admission history cannot leak between tests.
@pytest.fixture(autouse=True)
def isolated_rate_limiter(monkeypatch):

    monkeypatch.setattr(
        main_module,
        "chat_rate_limiter",
        SlidingWindowRateLimiter(limit=100, window_seconds=60),
    )
# Records request IDs, attempt numbers, and messages while returning predictable fake responses.
class RecordingFakeProvider(FakeProvider):
    # Initializes the list used to inspect provider calls.
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    # Records the request and attempt before returning the standard fake response.
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


# Rejects the first two attempt numbers and succeeds on the third to exercise retry recovery.
class FailTwiceFakeProvider(RecordingFakeProvider):
    # Records each call, fails attempts one and two, and returns a response on later attempts.
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


# Counts calls and always raises an uncertain failure to exercise unknown-outcome persistence.
class UnknownOutcomeFakeProvider:
    # Initializes the call counter used to detect unwanted retries of saved outcomes.
    def __init__(self) -> None:
        self.call_count = 0

    # Counts the call and raises a transient failure whose completion status is uncertain.
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


# Records calls and always raises a transient error to trigger fallback.
class AlwaysRetryFakeProvider:
    # Initializes the primary-provider call history for fallback assertions.
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    # Records the request and attempt, then raises a transient rejection.
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


# Records fallback calls and returns a distinctive provider identity and response.
class FallbackFakeProvider:
    # Initializes the call history used to inspect fallback execution.
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    # Records the fallback attempt and returns a recognizable response and provider identity.
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



# Builds a minimal valid chat payload with a supplied request ID and message.
def request_payload(request_id: str, message: str = "hello") -> dict[str, object]:
    return {
        "request_id": request_id,
        "message": message,
        "model_preference": "fast",
        "max_tokens": 50,
    }


# Gives each test its own SQLite database in pytest's temporary directory.
@pytest.fixture
def isolated_request_store(monkeypatch, tmp_path):
    database_path = tmp_path / "requests.sqlite3"
    monkeypatch.setattr(
        main_module,
        "request_store",
        RequestStore(database_path),
    )
    yield


# Checks response fields and confirms duplicates replay the saved result without another provider call.
def test_post_response_is_normalized_and_duplicate_replays_saved_response(
    monkeypatch: pytest.MonkeyPatch,
    isolated_request_store,
) -> None:
    provider = RecordingFakeProvider()
    monkeypatch.setattr(main_module, "primary_provider", provider)
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
    assert duplicate.json() == {**body, "cache_hit": True}
    assert len(provider.calls) == 1
    assert 'request_id="' + payload["request_id"] + '"' in metrics.text


# Checks that reusing a request ID with different input returns HTTP 409.
def test_reusing_request_id_with_different_payload_returns_conflict(
    monkeypatch: pytest.MonkeyPatch,
    isolated_request_store,
) -> None:
    provider = RecordingFakeProvider()
    monkeypatch.setattr(main_module, "primary_provider", provider)
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


# Checks that retries preserve the request ID and report the total attempt count.
def test_retry_attempts_reuse_request_id_and_report_attempt_count() -> None:
    provider = FailTwiceFakeProvider()
    request_id = f"pytest-retry-{uuid4().hex}"

    result = asyncio.run(
        llm_call(
            request_id=request_id,
            message="retry me",
            model_preference="fast",
            max_tokens=50,
            provider=provider,
            provider_name="fake",
            request_deadline_seconds=5,
            max_attempts=3,
            retry_base_delay_seconds=0,
        )
    )
    assert result.content == "recovered"
    assert result.attempts == 3
    assert [call[0] for call in provider.calls] == [request_id] * 3
    assert [call[1] for call in provider.calls] == [1, 2, 3]


# Checks that fallback continues within the original attempt budget.
def test_configured_fallback_uses_remaining_attempt_budget() -> None:
    # Simulates a primary provider that always rejects calls with a retryable error.
    class PrimaryProvider:
        # Raises a retryable rejection to force the gateway to consider fallback.
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

    # Checks the original request ID and second attempt number before returning a fallback result.
    class FallbackProvider:
        # Verifies request continuity and the second attempt number before returning a fallback result.
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

    result = asyncio.run(
        llm_call(
            request_id=expected_request_id,
            message="try fallback",
            model_preference="fast",
            max_tokens=50,
            provider=PrimaryProvider(),
            provider_name="fake",
            fallback_provider=FallbackProvider(),
            fallback_provider_name="fallback-provider",
            max_attempts=3,
            retry_base_delay_seconds=0,
        )
    )

    assert result.provider == "fallback-provider"
    assert result.model == "fallback-model"
    assert result.attempts == 2


# Checks that a primary failure switches providers and reports the fallback provider used.
def test_retry_uses_configured_fallback_and_reports_actual_provider() -> None:
    primary = AlwaysRetryFakeProvider()
    fallback = FallbackFakeProvider()
    request_id = f"pytest-fallback-{uuid4().hex}"

    result = asyncio.run(
        llm_call(
            request_id=request_id,
            message="fall back",
            model_preference="fast",
            max_tokens=50,
            provider=primary,
            provider_name="fake",
            fallback_provider=fallback,
            fallback_provider_name="fallback-fake",
            request_deadline_seconds=5,
            max_attempts=3,
            retry_base_delay_seconds=0,
        )
    )

    assert len(primary.calls) == 1
    assert fallback.calls == [(request_id, 2)]
    assert result.provider == "fallback-fake"
    assert result.attempts == 2


# Checks that uncertain outcomes are saved and duplicate requests do not restart generation.
def test_unknown_provider_outcome_is_persisted_and_not_called_again(
    monkeypatch: pytest.MonkeyPatch,
    isolated_request_store,
) -> None:
    provider = UnknownOutcomeFakeProvider()
    monkeypatch.setattr(main_module, "primary_provider", provider)
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


# Checks task-based model selection and the high-risk human-review flag.
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
    isolated_request_store,
    task_type: str | None,
    requested_preference: str,
    expected_model: str,
    human_review: bool,
) -> None:
    payload = request_payload(f"pytest-route-{uuid4().hex}")
    payload["model_preference"] = requested_preference
    if task_type is not None:
        payload["task_type"] = task_type

    response = TestClient(app).post("/chat", json=payload)

    assert response.status_code == 200
    assert response.json()["model"] == expected_model
    assert response.json()["human_review_required"] is human_review

# Checks that 12 competing threads admit exactly 3 requests under a shared rate limit.
def test_simultaneous_acquisition_respects_limit(limiter_clock):
    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=10)
    workers = 12
    barrier = Barrier(workers)

    # Waits for competing threads to reach the barrier before requesting a limiter slot.
    def acquire(_):
        barrier.wait(timeout=5)
        return limiter.try_acquire()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(acquire, range(workers)))

    assert results.count(None) == 3
    assert results.count(10) == 9

# Checks saved evaluation versions, model identity, result labels, and unavailable cost reporting.
def test_evaluation_runner_persists_versions_labels_and_usage(
    isolated_request_store,
) -> None:
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
        with TestClient(app) as client:
            summary = run_evaluation(
                dataset_path,
                root / "results",
                client=client,
                provider_name="fake",
            )
        saved_summary = json.loads(
            (root / "results" / f"{summary['run_id']}-summary.json").read_text(
                encoding="utf-8"
            )
        )
        assert saved_summary == summary
        result_path = next((root / "results").glob("*.jsonl"))
        result = json.loads(
            result_path.read_text(encoding="utf-8").splitlines()[0]
        )

    assert summary["dataset_version"] == "unit-v1"
    assert summary["prompt_version"] == "prompt-v7"
    assert summary["pass_rate"] == 1.0
    assert summary["estimated_cost_usd"] is None
    assert result["passed"] is True
    assert result["model_version"] == "fake-fast"
    assert result["known_failure_category"] == "instruction_following"
    assert result["grounding"]["faithfulness"] is None
    assert result["agentic_metrics"]["applicable"] is False


# Checks shared API breaker state and prevents same-backend fallback bypass.
@pytest.mark.parametrize("same_backend_fallback", [False, True])
def test_api_shares_circuit_state_across_requests(
    monkeypatch,
    isolated_request_store,
    same_backend_fallback,
):
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    primary = SimpleNamespace(complete=AsyncMock(side_effect=RetryableProviderError(
        "backend unavailable", status_code=503, failure_type="http_503"
    )))
    backup = RecordingFakeProvider()
    monkeypatch.setattr(main_module, "primary_provider", primary)
    monkeypatch.setattr(main_module, "primary_breaker", breaker)
    monkeypatch.setattr(main_module, "RETRY_BASE_DELAY_SECONDS", 0)
    if same_backend_fallback:
        monkeypatch.setattr(main_module, "backup_provider", backup)
        monkeypatch.setattr(main_module, "FALLBACK_PROVIDER", "fake")
        monkeypatch.setattr(main_module, "backup_breaker", breaker)

    with TestClient(app) as client:
        first = client.post("/chat", json=request_payload("trip-circuit"))
        second = client.post("/chat", json=request_payload("already-open-circuit"))

    assert first.status_code == second.status_code == 503
    assert first.json()["attempts"] == 1
    assert second.json()["attempts"] == 0
    assert breaker.state == "open"
    assert primary.complete.await_count == 1
    assert backup.calls == []


# Checks HTTP 429, rounded Retry-After, and rejection before database or provider work.
def test_rate_limit_returns_retry_after(
    monkeypatch,
    isolated_request_store,
    limiter_clock,
):
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=10)
    assert limiter.try_acquire() is None

    limiter_clock[0] = 100.2

    claim = Mock(side_effect=AssertionError("Database must not be called"))
    generate = AsyncMock(
        side_effect=AssertionError("Provider must not be called")
    )

    monkeypatch.setattr(main_module, "chat_rate_limiter", limiter)
    monkeypatch.setattr(main_module.request_store, "claim", claim)
    monkeypatch.setattr(main_module, "llm_call", generate)

    with TestClient(app) as client:
        response = client.post(
            "/chat",
            json=request_payload("rate-limit-header"),
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "10"
    assert response.json()["attempts"] == 0
    claim.assert_not_called()
    generate.assert_not_called()
