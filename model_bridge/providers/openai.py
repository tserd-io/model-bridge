import asyncio
import hashlib
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from model_bridge.providers.contracts import GenerationResult
from model_bridge.providers.contracts import PermanentProviderError
from model_bridge.providers.contracts import RetryableProviderError
from model_bridge.providers.pricing import estimate_cost_usd
from model_bridge.providers.transport import (
    run_with_attempt_timeout,
    is_retryable_status_code,
)


# Calls OpenAI Responses with tracing headers, timeout handling, and token-cost estimates.
class OpenAIProvider:
    # Initializes OpenAI model mappings and disables SDK retries so the gateway owns the retry budget.
    def __init__(self, models: dict[str, str]) -> None:
        self.client = AsyncOpenAI(max_retries=0)
        self.models = models

    # Calls OpenAI Responses with attempt tracing and returns normalized output, usage, and estimated cost.
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
        if request_id.isascii() and all(
            32 <= ord(character) <= 126 for character in request_id
        ):
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
                    extra_headers={"X-Client-Request-Id": provider_request_id},
                ),
                timeout,
            )
        except (APITimeoutError, asyncio.TimeoutError) as exc:
            # Normalize SDK and local timeouts as transient failures with uncertain completion.
            raise RetryableProviderError(
                "OpenAI request failed transiently",
                outcome_unknown=True,
                failure_type="timeout",
                provider="openai",
                model=model,
            ) from exc
        except APIConnectionError as exc:
            # Normalize transport failures while preserving the possibility of remote completion.
            raise RetryableProviderError(
                "OpenAI connection failed",
                outcome_unknown=True,
                failure_type="connection_error",
                provider="openai",
                model=model,
            ) from exc
        except APIStatusError as exc:
            # Translate configured transient statuses into retryable errors and other statuses into permanent rejections.
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
