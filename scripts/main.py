import asyncio
import time
import sqlite3
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from scripts.schemas import ChatRequest, ChatResponse
from scripts.generation import llm_call
from scripts.observability import log_event
from scripts.request_store import IdempotencyConflictError, RequestStore
from scripts.rate_limit import SlidingWindowRateLimiter
from scripts.providers import create_provider
from scripts.circuit_breaker import CircuitBreaker

from scripts.load_settings import (
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    FALLBACK_PROVIDER,
    IDEMPOTENCY_DB_PATH,
    LLM_PROVIDER,
    MAX_LLM_ATTEMPTS,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    REQUEST_DEADLINE_SECONDS,
    RETRY_BASE_DELAY_SECONDS,
)
from scripts.contracts import (
    PermanentProviderError,
    ProviderOutcomeUnknown,
    RetryableProviderError,
)
from scripts.chat_service import (
    _record_request_outcome,
    _route_model_preference,
    _state_result,
)


primary_provider = create_provider(LLM_PROVIDER)

backup_provider = (
    create_provider(FALLBACK_PROVIDER)
    if FALLBACK_PROVIDER
    else None
)

# Shares one process-local breaker per configured backend, including same-provider fallback.
provider_breakers = {
    name: CircuitBreaker(
        failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        cooldown_seconds=CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    )
    for name in {LLM_PROVIDER, FALLBACK_PROVIDER}
    if name is not None
}
primary_breaker = provider_breakers[LLM_PROVIDER]
backup_breaker = provider_breakers.get(FALLBACK_PROVIDER)

chat_rate_limiter = SlidingWindowRateLimiter(
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
)

app = FastAPI()
app.mount("/metrics", make_asgi_app())
request_store = RequestStore(IDEMPOTENCY_DB_PATH)
UNKNOWN_OUTCOME_DETAIL = (
    "The provider may have completed this request; do not resubmit it automatically"
)

# Reports that the app can respond without checking its dependencies.
@app.get("/health/live", tags=["health"])

async def liveness() -> dict[str, str]:
    return {"status": "alive"}

# Returns HTTP 200 for readable request storage or HTTP 503 when the check fails.
@app.get("/health/ready", tags=["health"])
async def readiness() -> JSONResponse:
    try:
        await asyncio.to_thread(request_store.check_readable)
    except sqlite3.Error:
        # Report storage as unavailable without exposing internal database error details.
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": {"database": "failed"}},
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ready", "checks": {"database": "ok"}},
    )

# Routes and rate-limits requests, handles deduplication, generates an answer, and saves its outcome.
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse | JSONResponse:
    started_at = time.perf_counter()
    model_preference = _route_model_preference(
        request.task_type,
        request.model_preference,
    )
    route = request.task_type or model_preference
    log_event(
        "request_started",
        request_id=request.request_id,
        provider=LLM_PROVIDER,
        route=route,
        tenant=request.tenant_id,
        model_preference=model_preference,
    )
    #limiter gate prevents user from overloading the app with requests
    retry_after = chat_rate_limiter.try_acquire()
    if retry_after is not None:
        response = _state_result(
            request=request,
            route=route,
            status="failed",
            detail="Rate limit exceeded; retry after the indicated delay",
            status_code=429,
            started_at=started_at,
            error_type="rate_limited",
        )
        response.headers["Retry-After"] = str(retry_after)
        return response   
    try:
        record = await asyncio.to_thread(
            request_store.claim,
            request.request_id,
            request.model_dump(mode="json"),
            REQUEST_DEADLINE_SECONDS + 5,
        )
    except IdempotencyConflictError as exc:
        # Return HTTP 409 because this request ID already represents different input.
        return _state_result(
            request,
            route,
            "failed",
            str(exc),
            409,
            started_at,
            error_type="idempotency_conflict",
        )

    if record.status == "success" and record.response is not None:
        response = ChatResponse(**record.response)
        _record_request_outcome(
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
        log_event("request_replayed", request_id=request.request_id, status="success")
        response.cache_hit = True
        return JSONResponse(
            status_code=200,
            content=response.model_dump(mode="json"),
            headers={"X-Idempotent-Replay": "true"},
        )

    if record.status == "in_progress":
        return _state_result(
            request,
            route,
            "in_progress",
            "A request with this ID is still being processed",
            202,
            started_at,
            record.attempts,
        )
    if record.status == "unknown":
        return _state_result(
            request,
            route,
            "unknown",
            record.detail or UNKNOWN_OUTCOME_DETAIL,
            202,
            started_at,
            record.attempts,
        )
    if record.status == "failed":
        return _state_result(
            request,
            route,
            "failed",
            record.detail or "The provider rejected the request",
            503,
            started_at,
            record.attempts,
        )

    try:
        result = await llm_call(
            request_id=request.request_id,
            message=request.message,
            model_preference=model_preference,
            max_tokens=request.max_tokens,
            provider=primary_provider,
            provider_name=LLM_PROVIDER,
            fallback_provider=backup_provider,
            fallback_provider_name=FALLBACK_PROVIDER,
            circuit_breaker=primary_breaker,
            fallback_circuit_breaker=backup_breaker,
            route=route,
            tenant_id=request.tenant_id,
            timeout_seconds=DEFAULT_LLM_TIMEOUT_SECONDS,
            request_deadline_seconds=REQUEST_DEADLINE_SECONDS,
            max_attempts=MAX_LLM_ATTEMPTS,
            retry_base_delay_seconds=RETRY_BASE_DELAY_SECONDS,
        )
    except ProviderOutcomeUnknown as exc:
        # Persist an uncertain outcome and return HTTP 202 without automatically resubmitting it.
        await asyncio.to_thread(
            request_store.mark_unknown,
            request.request_id,
            UNKNOWN_OUTCOME_DETAIL,
            exc.attempts,
        )
        return _state_result(
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
            request_store.mark_unknown,
            request.request_id,
            UNKNOWN_OUTCOME_DETAIL,
            0,
        )
        return _state_result(
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
                request_store.mark_unknown,
                request.request_id,
                detail,
                exc.attempts,
            )
        else:
            await asyncio.to_thread(
                request_store.mark_failed,
                request.request_id,
                detail,
                exc.attempts,
            )
        return _state_result(
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
            request_store.mark_failed,
            request.request_id,
            str(exc),
            exc.attempts,
        )
        return _state_result(
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
            request_store.mark_unknown,
            request.request_id,
            UNKNOWN_OUTCOME_DETAIL,
            0,
        )
        return _state_result(
            request,
            route,
            "unknown",
            UNKNOWN_OUTCOME_DETAIL,
            202,
            started_at,
            error_type=type(exc).__name__,
        )

    latency_ms = round((time.perf_counter() - started_at) * 1000)
    response = ChatResponse(
        request_id=request.request_id,
        status="success",
        provider=result.provider or LLM_PROVIDER,
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
        request_store.mark_success,
        request.request_id,
        response.model_dump(mode="json"),
    )
    _record_request_outcome(
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
    return response










