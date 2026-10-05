"""Chat execution and persistence, independent of FastAPI and HTTP objects."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Literal
from model_bridge.application.outcomes import ChatCommand, ChatOutcome, ChatResult
from model_bridge.config.models import Settings
from model_bridge.execution.generation import llm_call
from model_bridge.execution.circuit_breaker import CircuitBreaker
from model_bridge.execution.concurrency_limit import (
    ConcurrencyLimitExceeded,
    GenerationConcurrencyLimiter,
)
from model_bridge.execution.rate_limit import SlidingWindowRateLimiter
from model_bridge.observability.logging import log_event
from model_bridge.observability.metrics import record_request, track_generation
from model_bridge.providers.contracts import (
    GenerationResult,
    Provider,
    PermanentProviderError,
    ProviderOutcomeUnknown,
    RetryableProviderError,
)
from model_bridge.storage.request_store import IdempotencyConflictError, RequestStore

UNKNOWN_OUTCOME_DETAIL = (
    "The provider may have completed this request; do not resubmit it automatically"
)


# Uses task requirements to override the requested model preference when supplied.
def _route_model_preference(
    task_type: str | None,
    requested_preference: str,
) -> str:
    if task_type == "simple":
        return "fast"
    if task_type in {"complex", "high_risk"}:
        return "balanced"
    return requested_preference


# Builds a pending, uncertain, or failed response with routing and review metadata.
def _state_response(
    request: ChatCommand,
    status: Literal["in_progress", "unknown", "failed"],
    route: str,
    detail: str,
    attempts: int = 0,
) -> ChatResult:
    return ChatResult(
        request_id=request.request_id,
        status=status,
        detail=detail,
        attempts=attempts,
        route=route,
        task_type=request.task_type,
        tenant_id=request.tenant_id,
        human_review_required=request.task_type == "high_risk",
    )


# Owns injected collaborators and settings for the chat workflow.
@dataclass
class ChatService:
    settings: Settings
    request_store: RequestStore
    primary_provider: Provider
    backup_provider: Provider | None
    primary_breaker: CircuitBreaker
    backup_breaker: CircuitBreaker | None
    chat_rate_limiter: SlidingWindowRateLimiter
    generation_limiter: GenerationConcurrencyLimiter
    provider_name: str
    fallback_provider_name: str | None
    timeout_seconds: float
    deadline_seconds: float
    max_attempts: int
    retry_delay_seconds: float
    generate: Callable[..., Awaitable[GenerationResult]] = llm_call

    # Applies routing, admission, idempotency, generation and final persistence.
    async def handle(self, request: ChatCommand) -> ChatOutcome:
        started_at = time.perf_counter()
        tenant_limits = self.settings.effective_tenant_limits(
            request.tenant_id,
        )

        model_preference = _route_model_preference(
            request.task_type,
            request.model_preference,
        )
        route = request.task_type or model_preference
        log_event(
            "request_started",
            request_id=request.request_id,
            provider=self.provider_name,
            route=route,
            tenant=request.tenant_id,
            model_preference=model_preference,
        )
        # limiter gate prevents user from overloading the app with requests
        retry_after = self.chat_rate_limiter.try_acquire()
        if retry_after is not None:
            response = self._state_result(
                request=request,
                route=route,
                status="failed",
                detail="Rate limit exceeded; retry after the indicated delay",
                status_code=429,
                started_at=started_at,
                error_type="rate_limited",
            )
            response = replace(response, retry_after=retry_after)
            return response
        try:
            record = await asyncio.to_thread(
                self.request_store.claim,
                request.tenant_id,
                request.request_id,
                asdict(request),
                self.deadline_seconds + 5,
            )
        except IdempotencyConflictError as exc:
            # Return HTTP 409 because this request ID already represents different input.
            return self._state_result(
                request,
                route,
                "failed",
                str(exc),
                409,
                started_at,
                error_type="idempotency_conflict",
            )

        if record.status == "success" and record.response is not None:
            response = ChatResult(**record.response)
            self._record_request_outcome(
                request,
                route,
                "success",
                200,
                started_at,
                response.attempts,
                provider=response.provider,
                model=response.model,
                cache_hit=True,
            )
            log_event(
                "request_replayed",
                tenant=request.tenant_id,
                request_id=request.request_id,
                status="success",
            )
            return ChatOutcome(
                kind="success",
                response=replace(response, cache_hit=True),
                replayed=True,
            )

        if record.status == "in_progress":
            return self._state_result(
                request,
                route,
                "in_progress",
                "A request with this ID is still being processed",
                202,
                started_at,
                record.attempts,
            )
        if record.status == "unknown":
            return self._state_result(
                request,
                route,
                "unknown",
                record.detail or UNKNOWN_OUTCOME_DETAIL,
                202,
                started_at,
                record.attempts,
            )
        if record.status == "failed":
            return self._state_result(
                request,
                route,
                "failed",
                record.detail or "The provider rejected the request",
                503,
                started_at,
                record.attempts,
            )

        try:
            with (
                self.generation_limiter.slot(
                    tenant_id=request.tenant_id,
                    tenant_limit=tenant_limits.max_concurrent_jobs,
                ),
                track_generation(request.tenant_id),
            ):
                result = await self.generate(
                    request_id=request.request_id,
                    message=request.message,
                    model_preference=model_preference,
                    max_tokens=request.max_tokens,
                    provider=self.primary_provider,
                    provider_name=self.provider_name,
                    fallback_provider=self.backup_provider,
                    fallback_provider_name=self.fallback_provider_name,
                    circuit_breaker=self.primary_breaker,
                    fallback_circuit_breaker=self.backup_breaker,
                    route=route,
                    tenant_id=request.tenant_id,
                    timeout_seconds=self.timeout_seconds,
                    request_deadline_seconds=self.deadline_seconds,
                    max_attempts=self.max_attempts,
                    retry_base_delay_seconds=self.retry_delay_seconds,
                )
        except ConcurrencyLimitExceeded:
            detail = "Generation capacity is full; retry this request later"

            # Preserve request identity without recording a terminal failure.
            await asyncio.to_thread(
                self.request_store.mark_retryable,
                request.tenant_id,
                request.request_id,
                detail,
            )

            response = self._state_result(
                request=request,
                route=route,
                status="failed",
                detail=detail,
                status_code=503,
                started_at=started_at,
                attempts=0,
                error_type="concurrency_limited",
            )
            response = replace(response, retry_after=1)
            return response
        except ProviderOutcomeUnknown as exc:
            # Persist an uncertain outcome and return HTTP 202 without automatically resubmitting it.
            await asyncio.to_thread(
                self.request_store.mark_unknown,
                request.tenant_id,
                request.request_id,
                UNKNOWN_OUTCOME_DETAIL,
                exc.attempts,
            )
            return self._state_result(
                request,
                route,
                "unknown",
                UNKNOWN_OUTCOME_DETAIL,
                202,
                started_at,
                exc.attempts,
                error_type="unknown_outcome",
            )
        except asyncio.TimeoutError:
            # Conservatively save an unknown outcome with zero attempts because this exception has no count.
            await asyncio.to_thread(
                self.request_store.mark_unknown,
                request.tenant_id,
                request.request_id,
                UNKNOWN_OUTCOME_DETAIL,
                0,
            )
            return self._state_result(
                request,
                route,
                "unknown",
                UNKNOWN_OUTCOME_DETAIL,
                202,
                started_at,
                error_type="timeout",
                timed_out=True,
            )
        except RetryableProviderError as exc:
            # Use accumulated uncertainty to choose unknown/202 or failed/503 and preserve the attempt count.
            status = "unknown" if exc.outcome_unknown else "failed"

            if exc.outcome_unknown:
                detail = UNKNOWN_OUTCOME_DETAIL
            elif exc.failure_type == "deadline_exceeded":
                detail = "The request deadline left no time for another attempt"
            else:
                detail = "Provider rejected the request after the retry limit"

            status_code = 202 if exc.outcome_unknown else 503

            if exc.outcome_unknown:
                await asyncio.to_thread(
                    self.request_store.mark_unknown,
                    request.tenant_id,
                    request.request_id,
                    detail,
                    exc.attempts,
                )
            else:
                await asyncio.to_thread(
                    self.request_store.mark_failed,
                    request.tenant_id,
                    request.request_id,
                    detail,
                    exc.attempts,
                )
            return self._state_result(
                request,
                route,
                status,
                detail,
                status_code,
                started_at,
                exc.attempts,
                error_type=exc.failure_type,
                timed_out=exc.failure_type in {"timeout", "deadline_exceeded"},
            )
        except PermanentProviderError as exc:
            # Persist a definite rejection and return HTTP 502 with the recorded attempt count.
            await asyncio.to_thread(
                self.request_store.mark_failed,
                request.tenant_id,
                request.request_id,
                str(exc),
                exc.attempts,
            )
            return self._state_result(
                request,
                route,
                "failed",
                str(exc),
                502,
                started_at,
                exc.attempts,
                error_type="provider_rejection",
            )
        except Exception as exc:
            # Conservatively save unknown because an unexpected failure does not establish provider completion.
            await asyncio.to_thread(
                self.request_store.mark_unknown,
                request.tenant_id,
                request.request_id,
                UNKNOWN_OUTCOME_DETAIL,
                0,
            )
            return self._state_result(
                request,
                route,
                "unknown",
                UNKNOWN_OUTCOME_DETAIL,
                202,
                started_at,
                error_type=type(exc).__name__,
            )

        latency_ms = round((time.perf_counter() - started_at) * 1000)
        response = ChatResult(
            request_id=request.request_id,
            status="success",
            provider=result.provider or self.provider_name,
            model=result.model,
            content=result.content,
            latency_ms=latency_ms,
            attempts=result.attempts,
            route=route,
            task_type=request.task_type,
            tenant_id=request.tenant_id,
            human_review_required=request.task_type == "high_risk",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
        )
        await asyncio.to_thread(
            self.request_store.mark_success,
            request.tenant_id,
            request.request_id,
            asdict(response),
        )
        self._record_request_outcome(
            request,
            route,
            "success",
            200,
            started_at,
            result.attempts,
            provider=response.provider,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
        )
        return ChatOutcome(kind="success", response=response)

    # Measures request latency and writes outcome metrics and structured JSON logs.
    def _record_request_outcome(
        self,
        request: ChatCommand,
        route: str,
        status: str,
        status_code: int,
        started_at: float,
        attempts: int,
        provider: str | None = None,
        model: str | None = None,
        error_type: str = "none",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
        cache_hit: bool = False,
        timed_out: bool = False,
    ) -> int:
        latency_ms = round((time.perf_counter() - started_at) * 1000)
        provider_name = provider or self.provider_name
        record_request(
            request.request_id,
            provider_name,
            status,
            latency_ms,
            model=model,
            route=route,
            tenant=request.tenant_id,
            error_type=error_type,
            status_code=status_code,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            cache_hit=cache_hit,
            timed_out=timed_out,
        )
        log_event(
            "request_finished",
            request_id=request.request_id,
            provider=provider_name,
            model=model,
            route=route,
            tenant=request.tenant_id,
            status=status,
            status_code=status_code,
            attempts=attempts,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            cache_hit=cache_hit,
            error_type=error_type,
            timed_out=timed_out,
        )
        return latency_ms

    # Records a non-success outcome and carries delivery metadata to the API adapter.
    def _state_result(
        self,
        request: ChatCommand,
        route: str,
        status: Literal["in_progress", "unknown", "failed"],
        detail: str,
        status_code: int,
        started_at: float,
        attempts: int = 0,
        error_type: str = "none",
        timed_out: bool = False,
        cache_hit: bool = False,
    ) -> ChatOutcome:
        self._record_request_outcome(
            request,
            route,
            status,
            status_code,
            started_at,
            attempts,
            error_type=error_type,
            timed_out=timed_out,
            cache_hit=cache_hit,
        )
        kind = {409: "conflict", 429: "rate_limited", 502: "provider_rejected"}.get(
            status_code, status
        )
        return ChatOutcome(
            kind=kind,
            response=_state_response(request, status, route, detail, attempts),
            retry_after=1 if status == "in_progress" else None,
            error_type=error_type,
            timed_out=timed_out,
        )
