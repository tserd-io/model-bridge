import argparse
import asyncio
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

# Supports launching this file directly from the terminal.
project_root = str(Path(__file__).resolve().parents[1])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from model_bridge import main as main_module
from model_bridge.execution.circuit_breaker import CircuitBreaker
from model_bridge.execution.concurrency_limit import GenerationConcurrencyLimiter
from model_bridge.providers.contracts import GenerationResult
from model_bridge.providers.contracts import RetryableProviderError
from model_bridge.execution.generation import llm_call
from model_bridge.providers.openai import OpenAIProvider
from model_bridge.providers.ollama import OllamaProvider
from model_bridge.providers.transport import is_retryable_status_code
from model_bridge.providers.transport import run_with_attempt_timeout
from model_bridge.execution.rate_limit import TenantRateLimiter
from model_bridge.storage.request_store import RequestStore
from model_bridge.api.schemas import ChatRequest
from model_bridge.api.dependencies import AuthenticatedTenant, get_authenticated_tenant

SMOKE_TENANT = "test-tenant"
SMOKE_ATTEMPTS = 3


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
# Installs an isolated service and identity, restoring application state on exit.
@contextmanager
def smoke_client(provider, *, provider_name="fake", deterministic=True):
    async def authenticated_for_smoke() -> AuthenticatedTenant:
        return AuthenticatedTenant(id=SMOKE_TENANT)

    source = main_module.app.state.chat_service
    provider_settings = source.settings.providers[source.settings.primary_provider]
    with TemporaryDirectory() as directory:
        smoke_settings_data = source.settings.model_dump()
        smoke_settings_data["tenant_defaults"]["rate_limit"] = {
            "requests": 100,
            "window_seconds": 10,
        }
        smoke_settings_data["tenant_overrides"] = {}
        replacements = {
            "settings": type(source.settings).model_validate(smoke_settings_data),
            "request_store": RequestStore(Path(directory) / "requests.sqlite3"),
            "primary_provider": provider,
            "backup_provider": None,
            "provider_name": provider_name,
            "fallback_provider_name": None,
            "primary_breaker": CircuitBreaker(
                failure_threshold=5 if deterministic else provider_settings.circuit_breaker_failure_threshold,
                cooldown_seconds=provider_settings.circuit_breaker_cooldown_seconds,
            ),
            "backup_breaker": None,
            "chat_rate_limiter": TenantRateLimiter(100, 10),
            "generation_limiter": GenerationConcurrencyLimiter(1),
        }
        if deterministic:
            replacements.update({
                "max_attempts": SMOKE_ATTEMPTS,
                "timeout_seconds": 5,
                "deadline_seconds": 10,
                "retry_delay_seconds": 0,
            })
        service = replace(source, **replacements)
        with patch.object(main_module.app.state, "chat_service", service), patch.dict(
            main_module.app.dependency_overrides,
            {get_authenticated_tenant: authenticated_for_smoke},
        ), TestClient(main_module.app) as client:
            yield client

# Checks tenant-scoped replay, conflicts, pending/unknown states and durable storage.
def smoke_idempotency() -> None:
    provider = CountingProvider()
    with smoke_client(provider) as client:
        success_payload = {
            "request_id": f"smoke-success-{uuid4()}",
            "message": "hello", "model_preference": "fast", "max_tokens": 50,
        }
        first = client.post("/chat", json=success_payload)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "success"
        assert first.json()["cache_hit"] is False
        expected_replay = {**first.json(), "cache_hit": True}
        duplicate = client.post("/chat", json=success_payload)
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json() == expected_replay
        assert duplicate.headers.get("X-Idempotent-Replay") == "true"
        assert provider.call_count == 1

        metrics = client.get("/metrics/", headers={
            "Accept": "application/openmetrics-text; version=1.0.0; charset=utf-8",
        })
        if main_module.app.state.chat_service.settings.platform.metrics.enabled:
            assert metrics.status_code == 200, metrics.text
            assert f'request_id="{success_payload["request_id"]}"' in metrics.text
        else:
            assert metrics.status_code == 404

        conflict = client.post("/chat", json={**success_payload, "message": "different"})
        assert conflict.status_code == 409, conflict.text
        assert provider.call_count == 1

        pending_payload = {**success_payload, "request_id": f"smoke-pending-{uuid4()}"}
        normalized = ChatRequest(**pending_payload).model_copy(update={"tenant_id": SMOKE_TENANT})
        record = main_module.app.state.chat_service.request_store.claim(
            normalized.tenant_id, normalized.request_id,
            normalized.model_dump(mode="json"), 60,
        )
        assert record.status == "claimed"
        pending = client.post("/chat", json=pending_payload)
        assert pending.status_code == 202, pending.text
        assert pending.json()["status"] == "in_progress"
        assert pending.json()["attempts"] == 0
        assert provider.call_count == 1

        ambiguous = AmbiguousProvider()
        with patch.object(main_module.app.state.chat_service, "primary_provider", ambiguous):
            unknown_payload = {**success_payload, "request_id": f"smoke-unknown-{uuid4()}"}
            unknown = client.post("/chat", json=unknown_payload)
            assert unknown.status_code == 202, unknown.text
            assert unknown.json()["status"] == "unknown"
            assert unknown.json()["attempts"] == SMOKE_ATTEMPTS
            assert ambiguous.call_count == SMOKE_ATTEMPTS
            calls_before_replay = ambiguous.call_count
            duplicate_unknown = client.post("/chat", json=unknown_payload)
            assert duplicate_unknown.status_code == 202, duplicate_unknown.text
            assert duplicate_unknown.json() == unknown.json()
            assert ambiguous.call_count == calls_before_replay

        with patch.object(main_module.app.state.chat_service, "request_store", RequestStore(main_module.app.state.chat_service.request_store.database_path)):
            replayed = client.post("/chat", json=success_payload)
            assert replayed.status_code == 200, replayed.text
            assert replayed.json() == expected_replay
            assert replayed.headers.get("X-Idempotent-Replay") == "true"
            assert provider.call_count == 1
    print("Idempotency: replay, conflict, pending, unknown and database reopening passed.")

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
    



# Runs local checks by default; explicitly enables a configured-provider API call.
def main() -> None:
    parser = argparse.ArgumentParser(description="Model Bridge smoke checks")
    parser.add_argument("--live", action="store_true", help="Also call the configured primary provider")
    args = parser.parse_args()
    smoke_idempotency()
    smoke_retry_classification()
    smoke_openai_correlation_header()
    smoke_bounded_retries()
    smoke_timeout_limits()
    smoke_request_deadline_limits()
    if args.live:
        provider = main_module.app.state.chat_service.primary_provider
        provider_name = main_module.app.state.chat_service.provider_name
        with smoke_client(provider, provider_name=provider_name, deterministic=False) as client:
            response = client.post("/chat", json={
                "request_id": f"smoke-live-{uuid4()}",
                "message": "Reply with exactly: Model Bridge is working.",
                "model_preference": "fast", "max_tokens": 50,
            })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success" and body["content"]
        print(f"Configured provider {provider_name}: status={body['status']} model={body['model']}")
    print("Smoke run completed successfully.")


if __name__ == "__main__":
    main()
