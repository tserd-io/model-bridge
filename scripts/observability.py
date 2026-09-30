import json
import logging
from typing import Any

from prometheus_client import Counter, Histogram

logger = logging.getLogger("model_bridge")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(stream_handler)

LABEL_NAMES = ("provider", "model", "route", "tenant", "error_type", "status_code")

REQUESTS_TOTAL = Counter(
    "llm_requests",
    "Gateway requests by provider, model, route, tenant, error, and HTTP status",
    LABEL_NAMES,
)
SUCCESS_TOTAL = Counter("llm_success", "Successful LLM requests", LABEL_NAMES)
ERRORS_TOTAL = Counter("llm_errors", "Failed or unresolved LLM requests", LABEL_NAMES)
TIMEOUTS_TOTAL = Counter("llm_timeouts", "Requests ending in a timeout", LABEL_NAMES)
RETRIES_TOTAL = Counter("llm_retries", "Additional provider attempts", LABEL_NAMES)
FALLBACK_TOTAL = Counter(
    "llm_fallback",
    "Provider fallback selections",
    (*LABEL_NAMES, "fallback_provider"),
)
REQUEST_DURATION = Histogram(
    "llm_latency_seconds",
    "End-to-end LLM request latency",
    LABEL_NAMES,
)
TOKENS_TOTAL = Counter(
    "llm_tokens",
    "Provider-reported input and output tokens",
    (*LABEL_NAMES, "direction"),
)
COST_TOTAL = Counter(
    "llm_cost",
    "Estimated provider cost in USD where pricing is configured",
    LABEL_NAMES,
)
CACHE_HITS_TOTAL = Counter(
    "llm_cache_hits",
    "Idempotent responses served from stored results",
    LABEL_NAMES,
)
PROVIDER_ATTEMPTS_TOTAL = Counter(
    "llm_provider_attempts",
    "Individual provider calls",
    LABEL_NAMES,
)
PROVIDER_ERRORS_TOTAL = Counter(
    "llm_provider_errors",
    "Failed individual provider calls",
    LABEL_NAMES,
)
PROVIDER_TIMEOUTS_TOTAL = Counter(
    "llm_provider_timeouts",
    "Timed-out individual provider calls",
    LABEL_NAMES,
)


def _labels(
    provider: str,
    model: str | None,
    route: str,
    tenant: str,
    error_type: str | None,
    status_code: int,
) -> dict[str, str]:
    return {
        "provider": provider or "unknown",
        "model": model or "unknown",
        "route": route or "default",
        "tenant": tenant or "default",
        "error_type": error_type or "none",
        "status_code": str(status_code),
    }


def log_event(event: str, **fields: Any) -> None:
    logger.info(
        json.dumps(
            {"event": event, **fields},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def record_request(
    request_id: str,
    provider: str,
    status: str,
    latency_ms: float,
    model: str | None = None,
    route: str = "default",
    tenant: str = "default",
    error_type: str | None = None,
    status_code: int = 200,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
    cache_hit: bool = False,
    timed_out: bool = False,
) -> None:
    exemplar = {"request_id": request_id}
    labels = _labels(provider, model, route, tenant, error_type, status_code)
    REQUESTS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    REQUEST_DURATION.labels(**labels).observe(latency_ms / 1000, exemplar=exemplar)

    if status == "success":
        SUCCESS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    elif status != "in_progress":
        ERRORS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if timed_out:
        TIMEOUTS_TOTAL.labels(**labels).inc(exemplar=exemplar)

    if model is not None and input_tokens is not None:
        TOKENS_TOTAL.labels(**labels, direction="input").inc(input_tokens, exemplar=exemplar)
    if model is not None and output_tokens is not None:
        TOKENS_TOTAL.labels(**labels, direction="output").inc(output_tokens, exemplar=exemplar)
    if model is not None and estimated_cost_usd is not None:
        COST_TOTAL.labels(**labels).inc(estimated_cost_usd, exemplar=exemplar)
    if cache_hit:
        CACHE_HITS_TOTAL.labels(**labels).inc(exemplar=exemplar)


def record_provider_attempt(
    request_id: str,
    provider: str,
    model: str | None,
    route: str,
    tenant: str,
    status: str,
    error_type: str | None,
    status_code: int,
    attempt: int,
) -> None:
    exemplar = {"request_id": request_id}
    labels = _labels(provider, model, route, tenant, error_type, status_code)
    PROVIDER_ATTEMPTS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if attempt > 1:
        RETRIES_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if status != "success":
        PROVIDER_ERRORS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if error_type == "timeout":
        PROVIDER_TIMEOUTS_TOTAL.labels(**labels).inc(exemplar=exemplar)


def record_fallback(
    request_id: str,
    provider: str,
    model: str | None,
    route: str,
    tenant: str,
    error_type: str | None,
    status_code: int,
    fallback_provider: str,
) -> None:
    labels = _labels(provider, model, route, tenant, error_type, status_code)
    FALLBACK_TOTAL.labels(
        **labels,
        fallback_provider=fallback_provider or "unknown",
    ).inc(exemplar={"request_id": request_id})
