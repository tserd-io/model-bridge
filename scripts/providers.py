import asyncio
import hashlib
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from ollama import AsyncClient, ResponseError
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from scripts.config import (
    LLM_PROVIDER,
    MODEL_PREFERENCES,
    MODEL_PRICING_USD_PER_MILLION_TOKENS,
    OLLAMA_HOST,
)

T = TypeVar("T")


async def run_with_attempt_timeout(awaitable: Awaitable[T], timeout: float) -> T:
    return await asyncio.wait_for(awaitable, timeout=timeout)


class RetryableProviderError(Exception):
    """A transient provider failure that may succeed on a later attempt."""

    def __init__(
		self,
		message: str,
		attempts: int = 1,
		outcome_unknown: bool = False,
        failure_type: str = "provider_error",
        status_code: int = 0,
        provider: str | None = None,
        model: str | None = None,
	) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.outcome_unknown = outcome_unknown
        self.failure_type = failure_type
        self.status_code = status_code
        self.provider = provider
        self.model = model


class ProviderOutcomeUnknown(Exception):
    """The provider may have completed the request but its response was lost."""

    def __init__(self, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class PermanentProviderError(Exception):
    """The provider explicitly rejected a request that should not be retried."""

    def __init__(self, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def is_retryable_status_code(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


@dataclass(frozen=True)
class GenerationResult:
    content: str
    model: str
    attempts: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    provider: str | None = None


def estimate_cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    pricing = MODEL_PRICING_USD_PER_MILLION_TOKENS.get(model)
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    return (
        input_tokens * pricing["input"] + output_tokens * pricing["output"]
    ) / 1_000_000


class Provider(Protocol):
    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        ...


class OllamaProvider:
    def __init__(self, host: str, models: dict[str, str]) -> None:
        self.client = AsyncClient(host=host)
        self.models = models

    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        model = self.models[model_preference]
        try:
            response = await run_with_attempt_timeout(
                self.client.chat(
                    model=model,
                    messages=[{"role": "user", "content": message}],
                    options={"num_predict": max_tokens},
                ),
                timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RetryableProviderError(
				"Ollama request failed transiently",
				outcome_unknown=True,
				failure_type="timeout",
                provider="ollama",
                model=model,
			) from exc
        except ConnectionError as exc:
            raise RetryableProviderError(
				"Ollama connection failed",
				outcome_unknown=True,
				failure_type="connection_error",
                provider="ollama",
                model=model,
			) from exc
        except ResponseError as exc:
            if is_retryable_status_code(exc.status_code):
                raise RetryableProviderError(
                    f"Ollama returned retryable status {exc.status_code}",
					outcome_unknown=exc.status_code != 429,
					failure_type=f"http_{exc.status_code}",
					status_code=exc.status_code,
                    provider="ollama",
                    model=model,
                ) from exc
            raise PermanentProviderError(
                f"Ollama rejected the request with status {exc.status_code}"
            , attempts=1) from exc
        return GenerationResult(
            content=response["message"]["content"],
            model=model,
            input_tokens=response.get("prompt_eval_count"),
            output_tokens=response.get("eval_count"),
            provider="ollama",
        )


class OpenAIProvider:
    def __init__(self, models: dict[str, str]) -> None:
        self.client = AsyncOpenAI(max_retries=0)
        self.models = models

    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        model = self.models[model_preference]
        if request_id.isascii() and all(32 <= ord(character) <= 126 for character in request_id):
            correlation_token = request_id
        else:
            correlation_token = (
                f"gateway-{hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:24]}"
            )
        provider_request_id = f"{correlation_token}-attempt-{attempt}"
        try:
            response = await run_with_attempt_timeout(
                self.client.responses.create(
                    model=model,
                    input=message,
                    max_output_tokens=max_tokens,
                    extra_headers={
                        "X-Client-Request-Id": provider_request_id
                    },
                ),
                timeout,
            )
        except (APITimeoutError, asyncio.TimeoutError) as exc:
            raise RetryableProviderError(
				"OpenAI request failed transiently",
				outcome_unknown=True,
                failure_type="timeout",
                provider="openai",
                model=model,
			) from exc
        except APIConnectionError as exc:
            raise RetryableProviderError(
                "OpenAI connection failed",
                outcome_unknown=True,
                failure_type="connection_error",
                provider="openai",
                model=model,
            ) from exc
        except APIStatusError as exc:
            if is_retryable_status_code(exc.status_code):
                raise RetryableProviderError(
                    f"OpenAI returned retryable status {exc.status_code}",
					outcome_unknown=exc.status_code != 429,
                    failure_type=f"http_{exc.status_code}",
                    status_code=exc.status_code,
                    provider="openai",
                    model=model,
                ) from exc
            raise PermanentProviderError(
                f"OpenAI rejected the request with status {exc.status_code}"
            ) from exc
        usage = response.usage
        input_tokens = usage.input_tokens if usage is not None else None
        output_tokens = usage.output_tokens if usage is not None else None
        return GenerationResult(
            content=response.output_text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimate_cost_usd(
                model,
                input_tokens,
                output_tokens,
            ),
            provider="openai",
        )


class FakeProvider:
    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        return GenerationResult(
            content=f"Fake response: {message}",
            model=f"fake-{model_preference}",
            provider="fake",
        )


def create_provider(provider_name: str | None = None) -> Provider:
    selected_provider = (provider_name or LLM_PROVIDER).lower()
    models_by_provider = {
        "ollama": {
            preference: models["ollama"]
            for preference, models in MODEL_PREFERENCES.items()
        },
        "openai": {
            preference: models["openai"]
            for preference, models in MODEL_PREFERENCES.items()
        },
    }
    if selected_provider == "ollama":
        return OllamaProvider(host=OLLAMA_HOST, models=models_by_provider["ollama"])
    if selected_provider == "openai":
        return OpenAIProvider(models=models_by_provider["openai"])
    if selected_provider == "fake":
        return FakeProvider()
    raise ValueError(f"Unsupported LLM provider: {selected_provider}")