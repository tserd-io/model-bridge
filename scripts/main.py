import asyncio
import time
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field

from scripts.config import IDEMPOTENCY_DB_PATH, LLM_PROVIDER, REQUEST_DEADLINE_SECONDS
from scripts.functions import llm_call
from scripts.observability import log_event, record_request
from scripts.providers import (
    PermanentProviderError,
    ProviderOutcomeUnknown,
    RetryableProviderError,
)
from scripts.request_store import IdempotencyConflictError, RequestStore
import sqlite3


app = FastAPI()
app.mount("/metrics", make_asgi_app())
request_store = RequestStore(IDEMPOTENCY_DB_PATH)
UNKNOWN_OUTCOME_DETAIL = (
    "The provider may have completed this request; do not resubmit it automatically"
)

@app.get("/health/live", tags=["health"])

async def liveness() -> dict[str, str]:
    return {"status": "alive"}

def _check_database() -> None:
    # Open the existing database without accidentally creating a new one.
    database_uri = request_store.database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True, timeout=1)
    try:
        connection.execute("SELECT request_id FROM requests LIMIT 1").fetchone()
    finally:
        connection.close()

@app.get("/health/ready", tags=["health"])
async def readiness() -> JSONResponse:
    try:
        await asyncio.to_thread(_check_database)
    except sqlite3.Error:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": {"database": "failed"}},
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ready", "checks": {"database": "ok"}},
    )

class ChatRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    message: str = Field(min_length=1)
    model_preference: Literal["fast", "balanced"]
    max_tokens: int = Field(ge=1, le=8192)
    task_type: Literal["simple", "complex", "high_risk"] | None = None


class ChatResponse(BaseModel):
    request_id: str
    status: Literal["success", "in_progress", "unknown", "failed"]
    provider: str | None = None
    model: str | None = None
    content: str | None = None
    latency_ms: int | None = None
    attempts: int = 0
    detail: str | None = None
    route: str | None = None
    task_type: str | None = None
    tenant_id: str | None = None
    human_review_required: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cache_hit: bool = False


def _state_response(
    request: ChatRequest,
    status: Literal["in_progress", "unknown", "failed"],
    route: str,
    detail: str,
    attempts: int = 0,
) -> ChatResponse:
    return ChatResponse(
        request_id=request.request_id,
        status=status,
        detail=detail,
        attempts=attempts,
        route=route,
        task_type=request.task_type,
        tenant_id=request.tenant_id,
        human_review_required=request.task_type == "high_risk",
    )


def _record_request_outcome(
    request: ChatRequest,
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
    provider_name = provider or LLM_PROVIDER
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


def _route_model_preference(
    task_type: str | None,
    requested_preference: str,
) -> str:
    if task_type == "simple":
        return "fast"
    if task_type in {"complex", "high_risk"}:
        return "balanced"
    return requested_preference


def _state_result(
    request: ChatRequest,
    route: str,
    status: Literal["in_progress", "unknown", "failed"],
    detail: str,
    status_code: int,
    started_at: float,
    attempts: int = 0,
    error_type: str = "none",
    timed_out: bool = False,
    cache_hit: bool = False,
) -> JSONResponse:
    _record_request_outcome(
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
    response = _state_response(request, status, route, detail, attempts)
    headers = {"Retry-After": "1"} if status == "in_progress" else None
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json"),
        headers=headers,
    )


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

    try:
        record = await asyncio.to_thread(
            request_store.claim,
            request.request_id,
            request.model_dump(mode="json"),
            REQUEST_DEADLINE_SECONDS + 5,
        )
    except IdempotencyConflictError as exc:
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
            route=route,
            tenant_id=request.tenant_id,
        )
    except ProviderOutcomeUnknown as exc:
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
    except asyncio.TimeoutError as exc:
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
        status = "unknown" if exc.outcome_unknown else "failed"
        detail = UNKNOWN_OUTCOME_DETAIL if exc.outcome_unknown else "Provider rejected the request after the retry limit"
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
            timed_out=exc.failure_type == "timeout",
        )
    except PermanentProviderError as exc:
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
