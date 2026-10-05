import asyncio
import httpx
from ollama import AsyncClient, ResponseError
from model_bridge.providers.contracts import GenerationResult
from model_bridge.providers.contracts import PermanentProviderError
from model_bridge.providers.contracts import RetryableProviderError
from model_bridge.providers.transport import (
    run_with_attempt_timeout,
    is_retryable_status_code,
)


# Calls Ollama, enforces attempt timeouts, classifies errors, and normalizes responses.
class OllamaProvider:
    # Initializes the Ollama client and preference-to-model mapping.
    def __init__(self, host: str, models: dict[str, str]) -> None:
        self.client = AsyncClient(host=host)
        self.models = models

    # Calls the selected Ollama model, translates errors, and returns normalized text and usage.
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
        except (
            asyncio.TimeoutError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
        ) as exc:
            # Classify transport timeouts while preserving uncertain completion.
            raise RetryableProviderError(
                "Ollama request timed out",
                outcome_unknown=True,
                failure_type="timeout",
                provider="ollama",
                model=model,
            ) from exc

        except (
            ConnectionError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            # Classify network failures and invalid remote responses as transient.
            raise RetryableProviderError(
                "Ollama transport failed",
                outcome_unknown=True,
                failure_type="connection_error",
                provider="ollama",
                model=model,
            ) from exc
        except ResponseError as exc:
            # Translate transient HTTP statuses into retryable errors; only 429 is treated as a definite rejection here.
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
                f"Ollama rejected the request with status {exc.status_code}", attempts=1
            ) from exc
        return GenerationResult(
            content=response["message"]["content"],
            model=model,
            input_tokens=response.get("prompt_eval_count"),
            output_tokens=response.get("eval_count"),
            provider="ollama",
        )
