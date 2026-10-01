import asyncio
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

project_root = str(Path(__file__).resolve().parents[2])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from scripts.load_settings import LLM_PROVIDER, MAX_LLM_ATTEMPTS
import scripts.generation as generation
import scripts.main as main_module
from scripts.generation import llm_call
from scripts.main import app
from scripts.contracts import GenerationResult, RetryableProviderError
from scripts.providers import (
    OpenAIProvider,
    OllamaProvider,
    is_retryable_status_code,
    run_with_attempt_timeout,
)
from scripts.schemas import ChatRequest
from scripts.request_store import RequestStore

PROMPT_TEXT = "Reply with exactly: Ollama is working."
MODEL_PREFERENCE = "fast"


# Simulates a two-second completion while respecting the supplied attempt timeout.
class DelayedProvider:
    # Runs a simulated slow completion within the supplied attempt timeout.
    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        # Waits two seconds before producing a predictable response.
        async def delayed_completion() -> GenerationResult:
            await asyncio.sleep(2)
            return GenerationResult(
                content=f"Delayed response: {message}",
                model=f"delayed-{model_preference}",
            )

        return await run_with_attempt_timeout(delayed_completion(), timeout)


# Fails a configurable number of calls before succeeding to exercise retry behavior.
class FlakyProvider:
    # Configures how many calls fail before recovery and initializes the call counter.
    def __init__(self, failures_before_success: int) -> None:
        self.failures_before_success = failures_before_success
        self.call_count = 0

    # Counts calls, raises the configured transient failures, and then returns a response.
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
        if self.call_count <= self.failures_before_success:
            raise RetryableProviderError("simulated transient failure")
        return GenerationResult(
            content=f"Recovered response: {message}",
            model=f"flaky-{model_preference}",
        )


# Counts successful calls so duplicate requests can be checked for unnecessary generation.
class CountingProvider:
    # Initializes a counter for detecting repeated generation.
    def __init__(self) -> None:
        self.call_count = 0

    # Counts the call and returns a predictable result for replay checks.
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
        return GenerationResult(
            content=f"Saved response: {message}",
            model=f"counting-{model_preference}",
        )


# Counts calls and simulates timeouts where the provider outcome remains uncertain.
class AmbiguousProvider:
    # Initializes the count of simulated uncertain provider calls.
    def __init__(self) -> None:
        self.call_count = 0

    # Counts the call and simulates a timeout that may have completed remotely.
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
            "simulated provider timeout",
            outcome_unknown=True,
        )


# Checks replay, conflicting IDs, pending and uncertain outcomes, and persistence after reopening SQLite.
def smoke_idempotency() -> None:
    original_store = main_module.request_store
    original_provider = main_module.primary_provider
    original_backup = main_module.backup_provider
    with TemporaryDirectory() as directory:
        database_path = Path(directory) / "requests.sqlite3"
        main_module.request_store = RequestStore(database_path)
        provider = CountingProvider()
        main_module.primary_provider = provider
        main_module.backup_provider = None
        try:
            with TestClient(main_module.app) as client:
                success_payload = {
                    "request_id": "smoke-idempotent-success",
                    "message": "hello",
                    "model_preference": "fast",
                    "max_tokens": 50,
                }
                first = client.post("/chat", json=success_payload)
                duplicate = client.post("/chat", json=success_payload)
                if first.status_code != 200 or duplicate.json() != first.json():
                    raise AssertionError("Completed duplicate did not replay the saved response")
                if provider.call_count != 1:
                    raise AssertionError("Duplicate success called the provider again")

                metrics = client.get(
                    "/metrics",
                    headers={
                        "Accept": "application/openmetrics-text; version=1.0.0; charset=utf-8"
                    },
                )
                if (
                    metrics.status_code != 200
                    or 'request_id="smoke-idempotent-success"' not in metrics.text
                ):
                    raise AssertionError("Request ID was not exposed as a metrics exemplar")
                print("Correlation metrics: request ID present as an exemplar")

                conflict_payload = {**success_payload, "message": "different input"}
                conflict = client.post("/chat", json=conflict_payload)
                if conflict.status_code != 409:
                    raise AssertionError("Different payload with same ID was not rejected")

                in_progress_payload = {
                    "request_id": "smoke-idempotent-progress",
                    "message": "still working",
                    "model_preference": "fast",
                    "max_tokens": 50,
                }
                main_module.request_store.claim(
                    in_progress_payload["request_id"],
                    in_progress_payload,
                    60,
                )
                in_progress = client.post("/chat", json=in_progress_payload)
                if in_progress.status_code != 202 or in_progress.json()["status"] != "in_progress":
                    raise AssertionError("Duplicate in-progress request was not reported")

                unknown_provider = AmbiguousProvider()
                main_module.primary_provider = unknown_provider
                unknown_payload = {
                    "request_id": "smoke-idempotent-unknown",
                    "message": "maybe generated",
                    "model_preference": "fast",
                    "max_tokens": 50,
                }
                unknown = client.post("/chat", json=unknown_payload)
                unknown_duplicate = client.post("/chat", json=unknown_payload)
                if unknown.status_code != 202 or unknown.json()["status"] != "unknown":
                    raise AssertionError("Ambiguous provider outcome was not marked unknown")
                if (
                    unknown_duplicate.json() != unknown.json()
                    or unknown_provider.call_count != MAX_LLM_ATTEMPTS
                ):
                    raise AssertionError("Unknown duplicate started another provider call")
                if unknown.json()["attempts"] != MAX_LLM_ATTEMPTS:
                    raise AssertionError("Unknown response omitted the attempt count")

                main_module.request_store = RequestStore(database_path)
                replayed = client.post("/chat", json=success_payload)
                if replayed.json() != first.json():
                    raise AssertionError("Saved response did not persist after reopening the DB")
                print("Idempotency: replay, conflict, in-progress, and unknown checks passed")
        finally:
            # Restore the original database store and provider even when a smoke assertion fails.
            main_module.request_store = original_store
            main_module.primary_provider = original_provider
            main_module.backup_provider = original_backup


# Checks retryable error classification and uncertainty after transport failures.
def smoke_retry_classification() -> None:
    retryable = (408, 429, 500, 502, 503, 504)
    permanent = (400, 401, 403, 404, 422, 501)
    if not all(is_retryable_status_code(status) for status in retryable):
        raise AssertionError("A configured retryable status was classified as permanent")
    if any(is_retryable_status_code(status) for status in permanent):
        raise AssertionError("A permanent client error was classified as retryable")

    # Raises a supplied exception from chat calls to exercise the Ollama adapter's error classification.
    class FailingClient:
        # Stores the exception the fake client will raise.
        def __init__(self, error: Exception) -> None:
            self.error = error

        # Raises the injected exception to exercise the Ollama adapter error handler.
        async def chat(self, **kwargs):
            raise self.error

    for failure in (asyncio.TimeoutError("provider timeout"), ConnectionResetError()):
        provider = OllamaProvider.__new__(OllamaProvider)
        provider.client = FailingClient(failure)
        provider.models = {"fast": "test-model"}
        try:
            asyncio.run(
                provider.complete(
                    request_id="smoke-transport",
                    attempt=1,
                    message="hello",
                    model_preference="fast",
                    max_tokens=50,
                    timeout=1,
                )
            )
        except RetryableProviderError as exc:
            # Verify that the adapter preserved uncertainty for the injected transport failure.
            if not exc.outcome_unknown:
                raise AssertionError("Transport failure should retain its unknown outcome")
        else:
            raise AssertionError(f"{type(failure).__name__} was not classified as retryable")
    print("Retry classification: statuses, timeouts, and connection resets checked")


# Checks that OpenAI tracing headers contain the request ID and attempt number.
def smoke_openai_correlation_header() -> None:
    captured_request = {}

    # Captures outbound SDK arguments and supplies a minimal response stub.
    async def fake_create(**kwargs):
        captured_request.update(kwargs)
        return SimpleNamespace(output_text="stubbed OpenAI response", usage = None)

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(
        responses=SimpleNamespace(create=fake_create)
    )
    provider.models = {"fast": "gpt-5.5"}
    result = asyncio.run(
        provider.complete(
            request_id="req-123",
            attempt=2,
            message="hello",
            model_preference="fast",
            max_tokens=50,
            timeout=1,
        )
    )
    if captured_request["extra_headers"]["X-Client-Request-Id"] != "req-123-attempt-2":
        raise AssertionError("OpenAI tracing header did not include the request and attempt IDs")
    if result.content != "stubbed OpenAI response":
        raise AssertionError("OpenAI response normalization failed")
    print("OpenAI correlation header: request ID and attempt are attached")


# Checks recovery after transient failures and enforcement of the attempt limit.
def smoke_bounded_retries() -> None:
    provider = FlakyProvider(failures_before_success=2)

    result = asyncio.run(
        llm_call(
            request_id="smoke-retry-success",
            message="retry smoke test",
            model_preference="fast",
            max_tokens=50,
            provider=provider,
            provider_name="fake",
            request_deadline_seconds=10,
            max_attempts=3,
            retry_base_delay_seconds=0,
        )
    )

    if provider.call_count != 3 or result.attempts != 3:
        raise AssertionError("Expected success on the third provider attempt")

    provider.failures_before_success = 10
    provider.call_count = 0

    try:
        asyncio.run(
            llm_call(
                request_id="smoke-retry-exhausted",
                message="retry exhaustion smoke test",
                model_preference="fast",
                max_tokens=50,
                provider=provider,
                provider_name="fake",
                request_deadline_seconds=10,
                max_attempts=3,
                retry_base_delay_seconds=0,
            )
        )
    except RetryableProviderError:
        # Verify that retry exhaustion stopped at the configured call limit.
        if provider.call_count != 3:
            raise AssertionError("Expected the configured three-attempt cap")
    else:
        raise AssertionError("Expected transient failures to exhaust the attempt cap")

    print("Retries: recovery and attempt cap checked")


# Checks per-attempt timeout behavior using a provider with a controlled delay.
def smoke_timeout_limits() -> None:
    
    provider = DelayedProvider()
    
    for timeout_seconds in (1, 5, 10):
        started_at = time.monotonic()
        try:
            asyncio.run(
                llm_call(
                    request_id=f"smoke-attempt-timeout-{timeout_seconds}",
                    message="timeout smoke test",
                    model_preference="fast",
                    max_tokens=50,
                    timeout_seconds=timeout_seconds,
                    max_attempts=1,
                    retry_base_delay_seconds=0,
                    provider=provider,
                    provider_name="fake",
                )
            )
        except asyncio.TimeoutError:
            # Recognize a raw timeout as a timed-out completion.
            timed_out = True
        except RetryableProviderError as exc:
            # Interpret the wrapped uncertain failure from the simulated provider as a timeout.
            timed_out = exc.outcome_unknown
        else:
            timed_out = False
        elapsed = time.monotonic() - started_at
        if timed_out != (timeout_seconds < 2):
            raise AssertionError(f"Unexpected per-attempt timeout at {timeout_seconds}s")
        print(f"Attempt timeout {timeout_seconds}s: {elapsed:.2f}s")
    


# Checks overall request-deadline behavior using a provider with a controlled delay.
def smoke_request_deadline_limits() -> None:
    
    provider = DelayedProvider()
    
    for deadline_seconds in (1, 5, 10):
        try:
            asyncio.run(
                llm_call(
                    request_id=f"smoke-overall-deadline-{deadline_seconds}",
                    message="overall deadline smoke test",
                    model_preference="fast",
                    max_tokens=50,
                    timeout_seconds=30,
                    request_deadline_seconds=deadline_seconds,
                    provider=provider,
                    provider_name="fake",
                )
            )
        except asyncio.TimeoutError:
            # Recognize a raw deadline timeout; structured deadline errors are not caught by this block.
            timed_out = True
        except RetryableProviderError as exc:
            # Recognize the structured timeout or deadline error from generation.
            timed_out = exc.failure_type in {"timeout", "deadline_exceeded"}
        else:
            timed_out = False
        if timed_out != (deadline_seconds < 2):
            raise AssertionError(f"Unexpected overall deadline at {deadline_seconds}s")
        print(f"Overall deadline {deadline_seconds}s: {'timeout' if timed_out else 'completed'}")
    


# Sends a sample request through the configured provider, then runs the smoke routines.
def main() -> None:
    request_id = f"smoke-{uuid4()}"
    print(f"Testing {LLM_PROVIDER} with the {MODEL_PREFERENCE} preference")
    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={
                "request_id": request_id,
                "message": PROMPT_TEXT,
                "model_preference": MODEL_PREFERENCE,
                "max_tokens": 500,
            },
        )
    response.raise_for_status()
    body = response.json()
    print(f"request_id={body['request_id']} status={body['status']} model={body['model']}")
    print(f"latency_ms={body['latency_ms']} attempts={body['attempts']} content={body['content']}")
    smoke_idempotency()
    smoke_retry_classification()
    smoke_openai_correlation_header()
    smoke_bounded_retries()
    smoke_timeout_limits()
    smoke_request_deadline_limits()
    print("Smoke run completed successfully.")


if __name__ == "__main__":
    main()
