"""SDK translation checks retained from the former manual smoke runner."""

import asyncio
from types import SimpleNamespace
from model_bridge.providers.contracts import RetryableProviderError
from model_bridge.providers.openai import OpenAIProvider
from model_bridge.providers.ollama import OllamaProvider
from model_bridge.providers.transport import is_retryable_status_code


def test_retry_classification() -> None:
    retryable = (408, 429, 500, 502, 503, 504)
    permanent = (400, 401, 403, 404, 422, 501)
    if not all(is_retryable_status_code(status) for status in retryable):
        raise AssertionError(
            "A configured retryable status was classified as permanent"
        )
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
                raise AssertionError(
                    "Transport failure should retain its unknown outcome"
                )
        else:
            raise AssertionError(
                f"{type(failure).__name__} was not classified as retryable"
            )
    print("Retry classification: statuses, timeouts, and connection resets checked")


def test_openai_correlation_header() -> None:
    captured_request = {}

    # Captures outbound SDK arguments and supplies a minimal response stub.
    async def fake_create(**kwargs):
        captured_request.update(kwargs)
        return SimpleNamespace(output_text="stubbed OpenAI response", usage=None)

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))
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
        raise AssertionError(
            "OpenAI tracing header did not include the request and attempt IDs"
        )
    if result.content != "stubbed OpenAI response":
        raise AssertionError("OpenAI response normalization failed")
    print("OpenAI correlation header: request ID and attempt are attached")
