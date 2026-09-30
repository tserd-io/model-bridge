import asyncio
import time
import random
from dataclasses import replace

from scripts.config import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    FALLBACK_PROVIDER,
    LLM_PROVIDER,
    MAX_LLM_ATTEMPTS,
    MODEL_PREFERENCES,
    REQUEST_DEADLINE_SECONDS,
    RETRY_BASE_DELAY_SECONDS,
)
from scripts.observability import (
    log_event,
    record_fallback,
    record_provider_attempt,
)
from scripts.providers import create_provider
from scripts.providers import GenerationResult, ProviderOutcomeUnknown, RetryableProviderError
provider = create_provider()
fallback_provider = create_provider(FALLBACK_PROVIDER) if FALLBACK_PROVIDER else None

async def llm_call(
    request_id: str,
    message: str,
    model_preference: str,
    max_tokens: int,
    route: str = "default",
    tenant_id: str = "default",
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    request_deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
    max_attempts: int = MAX_LLM_ATTEMPTS,
    retry_base_delay_seconds: float = RETRY_BASE_DELAY_SECONDS,
) -> GenerationResult:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + request_deadline_seconds
    last_error: Exception | None = None
    active_provider = provider
    active_provider_name = LLM_PROVIDER
    fallback_used = False

    for attempt in range(1, max_attempts + 1):
        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            raise asyncio.TimeoutError("LLM request deadline expired") from last_error

        attempt_timeout = min(timeout_seconds, remaining_seconds)
        attempt_started = time.perf_counter()
        model_name = _metric_model(active_provider_name, model_preference)
        log_event(
            "provider_attempt_started",
            request_id=request_id,
            provider=active_provider_name,
            model=model_name,
            model_preference=model_preference,
            route=route,
            tenant=tenant_id,
            attempt=attempt,
            timeout_ms=round(attempt_timeout * 1000),
        )
        try:
            result = await active_provider.complete(
                request_id=request_id,
                attempt=attempt,
                message=message,
                model_preference=model_preference,
                max_tokens=max_tokens,
                timeout=attempt_timeout,
            )
        except ProviderOutcomeUnknown as exc:
            _record_attempt(
                request_id,
                active_provider_name,
                attempt,
                "unknown",
                attempt_started,
                model=model_name,
                error=exc,
                route=route,
                tenant=tenant_id,
                status_code=getattr(exc, "status_code", 0),
            )
            raise ProviderOutcomeUnknown(str(exc), attempts=attempt) from exc
        except RetryableProviderError as exc:
            _record_attempt(
                request_id,
                active_provider_name,
                attempt,
                "retryable_error",
                attempt_started,
                model=model_name,
                error=exc,
                route=route,
                tenant=tenant_id,
                status_code=exc.status_code,
            )
            last_error = exc
            if fallback_provider is not None and not fallback_used:
                active_model = _metric_model(active_provider_name, model_preference)
                record_fallback(
                    request_id,
                    active_provider_name,
                    active_model,
                    route,
                    tenant_id,
                    exc.failure_type,
                    exc.status_code,
                    FALLBACK_PROVIDER,
                )
                log_event(
                    "provider_fallback_selected",
                    request_id=request_id,
                    provider_from=active_provider_name,
                    provider_to=FALLBACK_PROVIDER,
                    route=route,
                    tenant=tenant_id,
                    attempt=attempt + 1,
                    error_type=exc.failure_type,
                    status_code=exc.status_code,
                )
                active_provider = fallback_provider
                active_provider_name = FALLBACK_PROVIDER
                fallback_used = True
                continue
            if attempt == max_attempts:
                raise RetryableProviderError(
					str(exc),
					attempts=attempt,
					outcome_unknown=exc.outcome_unknown,
                    failure_type=exc.failure_type,
                    status_code=exc.status_code,
                    provider=active_provider_name,
                    model=model_name,
				) from exc

            remaining_seconds = deadline - loop.time()
            backoff_cap = retry_base_delay_seconds * (2 ** (attempt - 1))
            delay_seconds = random.uniform(0, backoff_cap)
            if delay_seconds >= remaining_seconds:
                raise asyncio.TimeoutError("LLM request deadline expired") from exc
            await asyncio.sleep(delay_seconds)
        except asyncio.TimeoutError as exc:
            wrapped_error = RetryableProviderError(
                "Provider call timed out",
                attempts=attempt,
                outcome_unknown=True,
            )
            _record_attempt(
                request_id,
                active_provider_name,
                attempt,
                "retryable_error",
                attempt_started,
                model=model_name,
                error=wrapped_error,
                route=route,
                tenant=tenant_id,
                status_code=0,
            )
            last_error = wrapped_error
            if fallback_provider is not None and not fallback_used:
                active_model = _metric_model(active_provider_name, model_preference)
                record_fallback(
                    request_id,
                    active_provider_name,
                    active_model,
                    route,
                    tenant_id,
                    "timeout",
                    0,
                    FALLBACK_PROVIDER,
                )
                log_event(
					"provider_fallback_selected",
					request_id=request_id,
					provider_from=active_provider_name,
					provider_to=FALLBACK_PROVIDER,
                    route=route,
                    tenant=tenant_id,
					attempt=attempt + 1,
					error_type=type(exc).__name__,
                    status_code=0,
				)
                active_provider = fallback_provider
                active_provider_name = FALLBACK_PROVIDER
                fallback_used = True
                continue
            if attempt == max_attempts:
                raise RetryableProviderError(
                    str(wrapped_error),
                    attempts=attempt,
                    outcome_unknown=True,
                    failure_type="timeout",
                    provider=active_provider_name,
                    model=model_name,
                ) from exc
            remaining_seconds = deadline - loop.time()
            backoff_cap = retry_base_delay_seconds * (2 ** (attempt - 1))
            delay_seconds = random.uniform(0, backoff_cap)
            if delay_seconds >= remaining_seconds:
                raise asyncio.TimeoutError("LLM request deadline expired") from exc
            await asyncio.sleep(delay_seconds)
        except Exception as exc:
            _record_attempt(
				request_id,
				active_provider_name,
				attempt,
				"error",
				attempt_started,
                model=model_name,
				error=exc,
                route=route,
                tenant=tenant_id,
                status_code=0,
			)
            raise
        else:
            _record_attempt(
                request_id,
                active_provider_name,
                attempt,
                "success",
                attempt_started,
                model=result.model,
                route=route,
                tenant=tenant_id,
                status_code=200,
            )
            return replace(
                result,
                attempts=attempt,
                provider=result.provider or active_provider_name,
            )

    raise RuntimeError("LLM call ended without a result")


def _record_attempt(
    request_id: str,
    provider_name: str,
    attempt: int,
    status: str,
    started_at: float,
    model: str | None = None,
    error: Exception | None = None,
    route: str = "default",
    tenant: str = "default",
    status_code: int | None = None,
) -> None:
    latency_ms = round((time.perf_counter() - started_at) * 1000)
    error_type = (
        getattr(error, "failure_type", type(error).__name__)
        if error is not None
        else "none"
    )
    resolved_model = model or _metric_model(provider_name, "unknown")
    resolved_status_code = status_code or getattr(error, "status_code", 0)
    record_provider_attempt(
        request_id,
        provider_name,
        resolved_model,
        route,
        tenant,
        status,
        error_type,
        resolved_status_code,
        attempt,
    )
    log_event(
        "provider_attempt_finished",
        request_id=request_id,
        provider=provider_name,
        attempt=attempt,
        status=status,
        route=route,
        tenant=tenant,
        latency_ms=latency_ms,
        model=model or resolved_model,
        error_type=error_type,
        status_code=resolved_status_code,
    )


def _metric_model(provider_name: str, model_preference: str) -> str:
    if provider_name == "fake":
        return f"fake-{model_preference}"
    return MODEL_PREFERENCES.get(model_preference, {}).get(provider_name, "unknown")
