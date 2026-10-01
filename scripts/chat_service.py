import time
from typing import Literal

from fastapi.responses import JSONResponse

from scripts.load_settings import LLM_PROVIDER
from scripts.observability import log_event, record_request
from scripts.schemas import ChatRequest, ChatResponse
# Measures request latency and writes outcome metrics and structured JSON logs.
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


# Records a non-success outcome and builds its HTTP response and polling header.
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
# Builds a pending, uncertain, or failed response with routing and review metadata.
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