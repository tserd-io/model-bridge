from contextlib import contextmanager
from prometheus_client import Counter, Gauge, Histogram
from model_bridge.observability.labels import (
    LABEL_NAMES,
    METRICS_SETTINGS,
    metric_tenant,
    _labels,
)

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
ACTIVE_GENERATIONS = Gauge(
    "llm_active_generations",
    "Generation jobs currently holding capacity in this process",
    ("tenant",),
)


# Tracks admitted generation jobs and releases the gauge on every exit.
@contextmanager
def track_generation(tenant: str):
    if not METRICS_SETTINGS.enabled:
        yield
        return

    with ACTIVE_GENERATIONS.labels(
        tenant=metric_tenant(tenant),
    ).track_inprogress():
        yield


# Updates request counts, latency, usage, and cache metrics with a request-ID exemplar.
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
    if not METRICS_SETTINGS.enabled:
        return
    exemplar = {"request_id": request_id}
    labels = _labels(
        provider,
        model,
        route,
        tenant,
        error_type,
        status_code,
    )
    REQUESTS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    REQUEST_DURATION.labels(**labels).observe(latency_ms / 1000, exemplar=exemplar)

    if status == "success":
        SUCCESS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    elif status != "in_progress":
        ERRORS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if timed_out:
        TIMEOUTS_TOTAL.labels(**labels).inc(exemplar=exemplar)

    if model is not None and input_tokens is not None:
        TOKENS_TOTAL.labels(**labels, direction="input").inc(
            input_tokens, exemplar=exemplar
        )
    if model is not None and output_tokens is not None:
        TOKENS_TOTAL.labels(**labels, direction="output").inc(
            output_tokens, exemplar=exemplar
        )
    if model is not None and estimated_cost_usd is not None:
        COST_TOTAL.labels(**labels).inc(estimated_cost_usd, exemplar=exemplar)
    if cache_hit:
        CACHE_HITS_TOTAL.labels(**labels).inc(exemplar=exemplar)


# Counts provider attempts, retries, errors, and timeouts with request correlation.
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
    if not METRICS_SETTINGS.enabled:
        return
    exemplar = {"request_id": request_id}
    labels = _labels(provider, model, route, tenant, error_type, status_code)
    PROVIDER_ATTEMPTS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if attempt > 1:
        RETRIES_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if status != "success":
        PROVIDER_ERRORS_TOTAL.labels(**labels).inc(exemplar=exemplar)
    if error_type == "timeout":
        PROVIDER_TIMEOUTS_TOTAL.labels(**labels).inc(exemplar=exemplar)


# Counts a fallback selection and identifies its source and destination providers.
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
    if not METRICS_SETTINGS.enabled:
        return
    labels = _labels(provider, model, route, tenant, error_type, status_code)
    FALLBACK_TOTAL.labels(
        **labels,
        fallback_provider=fallback_provider or "unknown",
    ).inc(exemplar={"request_id": request_id})
