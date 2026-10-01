import asyncio
import random
import time
from dataclasses import replace

from scripts.load_settings import MODEL_PREFERENCES
from scripts.observability import (
    log_event,
    record_fallback,
    record_provider_attempt,
)
from scripts.contracts import (
    GenerationResult,
    PermanentProviderError,
    Provider,
    ProviderOutcomeUnknown,
    RetryableProviderError,
)

async def complete_attempt(
    provider: Provider,
    *,
    request_id: str,
    attempt: int,
    message: str,
    model_preference: str,
    max_tokens: int,
    timeout: float,
) -> GenerationResult:
    """Convert raw timeouts into the gateway's standard transient error."""
    try:
        return await provider.complete(
            request_id=request_id,
            attempt=attempt,
            message=message,
            model_preference=model_preference,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise RetryableProviderError(
            "Provider call timed out",
            attempts=attempt,
            outcome_unknown=True,
            failure_type="timeout",
        ) from exc
#def create_app():
# Coordinates generation attempts, deadlines, backoff, and a bounded provider fallback.
async def llm_call(
    request_id: str,
    message: str,
    model_preference: str,
    max_tokens: int,
    *,
    provider: Provider,
    provider_name: str,
    fallback_provider: Provider | None = None,
    fallback_provider_name: str | None = None,
    route: str = "default",
    tenant_id: str = "default",
    timeout_seconds: float = 30,
    request_deadline_seconds: float = 45,
    max_attempts: int = 3,
    retry_base_delay_seconds: float = 0.25,
) -> GenerationResult:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if fallback_provider is not None and not fallback_provider_name:
        raise ValueError(
            "fallback_provider_name is required when a fallback provider is supplied"
        )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request_deadline_seconds
    last_error: Exception | None = None
    active_provider = provider
    active_provider_name = provider_name
    fallback_used = False
    any_outcome_unknown = False

    # Preserves completed attempt count and earlier uncertainty when no retry time remains.
    def deadline_error(attempts: int) -> RetryableProviderError:
        
        return RetryableProviderError(
            "The request deadline leaves no time for another attempt",
            attempts=attempts,
            outcome_unknown=any_outcome_unknown,
            failure_type="deadline_exceeded",
        )

    for attempt in range(1, max_attempts + 1):
        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            raise deadline_error(attempt - 1) from last_error

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
            result = await complete_attempt(
                active_provider,
                request_id=request_id,
                attempt=attempt,
                message=message,
                model_preference=model_preference,
                max_tokens=max_tokens,
                timeout=attempt_timeout,
                )
        except ProviderOutcomeUnknown as exc:
            # Stop further attempts because completion is explicitly uncertain; preserve the attempt count.
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
            # Accumulate uncertainty, check the remaining budgets, then select fallback or back off.
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
            any_outcome_unknown = (any_outcome_unknown or exc.outcome_unknown)
            attempts_exhausted = attempt >= max_attempts
            deadline_expired = loop.time() >= deadline
            if deadline_expired:
                raise deadline_error(attempt) from exc
            if attempts_exhausted:
                raise RetryableProviderError(
                    str(exc),
                    attempts=attempt,
                    outcome_unknown=any_outcome_unknown,
                    failure_type=exc.failure_type,
                    status_code=exc.status_code,
                    provider=active_provider_name,
                    model=model_name,
                ) from exc
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
                    fallback_provider_name,
                )
                log_event(
                    "provider_fallback_selected",
                    request_id=request_id,
                    provider_from=active_provider_name,
                    provider_to=fallback_provider_name,
                    route=route,
                    tenant=tenant_id,
                    attempt=attempt + 1,
                    error_type=exc.failure_type,
                    status_code=exc.status_code,
                )
                active_provider = fallback_provider
                active_provider_name = fallback_provider_name
                fallback_used = True
                continue

            remaining_seconds = deadline - loop.time()
            backoff_cap = retry_base_delay_seconds * (2 ** (attempt - 1))
            delay_seconds = random.uniform(0, backoff_cap)
            if delay_seconds >= remaining_seconds:
                raise deadline_error(attempt) from exc
            await asyncio.sleep(delay_seconds)
        except PermanentProviderError as exc:
            # Stop retrying a definite rejection without erasing uncertainty from earlier attempts.
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

            if any_outcome_unknown:
                raise ProviderOutcomeUnknown(
                    "An earlier provider attempt may have completed",
                    attempts=attempt,
                ) from exc

            raise PermanentProviderError(
                str(exc),
                attempts=attempt,
            ) from exc
        except Exception as exc:
            # Record unexpected failures and propagate them to the API outcome handler.
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


# Records the duration, outcome, and correlation details of one provider attempt.
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


# Resolves a configured model name for metrics, including fake and unknown providers.
def _metric_model(provider_name: str, model_preference: str) -> str:
    if provider_name == "fake":
        return f"fake-{model_preference}"
    return MODEL_PREFERENCES.get(model_preference, {}).get(provider_name, "unknown")
