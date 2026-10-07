# Proposed runtime policy implementation

## Test implementation status

Finished on 2026-10-07: all test changes proposed in this document have been
implemented in the test suite. Their proposal sections have been removed.

- [x] Shared fixtures and fake-provider smoke checks.
- [x] Service, gateway, circuit-breaker, and concurrency test collaborators.
- [x] Generation deadline, timeout, and probe-release regression tests.
- [x] Custom input/token limits and invalid-Unicode regression tests.
- [x] Logger restoration, shutdown assertions, and application/settings consistency tests.

Finished means the tests are implemented, not that runtime hardening is complete.
The current application still lacks proposed runtime behavior, including
`TenantRateLimiter`, which prevents full-suite collection. Runtime changes below
remain proposals; no production code was changed as part of this test-only work.

Review proposal for the remaining runtime changes only.

The following snippets show the proposed code with surrounding context. Above each code block, current line ranges identify its location in the unchanged source and proposed ranges identify its location in the complete revised file. Apply the snippets together, accounting for line shifts; they are excerpts rather than separate standalone files. The new request_policy.py module is shown in full.

## Runtime behavior and boundaries

1. **Output tokens:** reject requests above the effective tenant or platform ceiling before rate admission, storage, or generation. No silent clamping. HTTP returns 422; application calls receive `invalid_request`.
2. **Input size:** check both actual HTTP bytes and normalized caller-controlled fields. Trusted tenant identity and transport metadata do not affect normalized size or alter the existing storage fingerprint. Oversized input returns 413 / `input_too_large`.
3. **Generation deadline:** each call to generation receives its tenant's budget. The monotonic deadline is created at generation start and shared across attempts, delays, and fallback, with time rechecked after breaker acquisition and logging. This is a generation deadline, not an HTTP or storage deadline.
4. **Rate admission:** use one lock to check and spend tenant and platform quotas together. Denials spend neither. Replay, conflicts, and later provider failures still count as admitted requests. Clean expired histories on the next admission; no background thread. These limits remain process-local.
5. **Logging:** configure the process-wide application logger once when composing the app. Apply enabled/minimum severity, use explicit event severities and UTC timestamps, and continue logging operational fields without prompt, generated content, credentials, or exception text. Uvicorn's own logger remains separate. Callers of the general log_event helper must continue to supply only permitted operational fields.
6. **SQLite:** pass configured busy timeout into write connections and derive processing leases from the tenant's generation budget plus the configured margin. Keep readiness's independent one-second read timeout. Existing request keys, hashes, and state transitions are preserved.
7. **Shutdown:** introduce `run_server` and use it as Docker's command. One worker receives configured graceful shutdown seconds. Keep the imported `main:app` entry point available. The module guard avoids constructing two applications when started with `python -m model_bridge.main`.
8. **Provider timeouts:** switch the attempt timeout when switching providers, always capped by remaining generation time. Attempts and backoff remain one shared budget using the primary retry settings. Fallback does not receive a fresh attempt count or deadline. Wrap provider calls using the existing transport timeout helper, so an adapter ignoring its timeout parameter is still cancelled. As with other asyncio timeouts, adapters must cooperate with cancellation.

## Modular design and self-review

- Add only one runtime module: `application/request_policy.py`. It performs deterministic input checks and returns semantic policy violations; it imports neither FastAPI nor storage/provider code.
- Keep request-body measurement in the HTTP adapter and pass the byte count as a keyword to `handle`. Do not add it to ChatCommand, so serialization and replay hashes are unchanged.
- Construct HTTP schemas per application from its settings. This preserves FastAPI field-validation responses and supports injected settings with ceilings above or below the previous fixed defaults. The application still checks policy independently for direct service calls.
- Define `TenantAdmissionLimiter` as the service's structural interface; keep the existing standalone SlidingWindowRateLimiter for its separate unit use. Production wiring uses TenantRateLimiter. Test collaborators are updated to the new boundary instead of adding production special cases for old fakes.
- Reject changes to a tenant's live rate policy while history remains active. Settings are startup configuration; restarting adopts a new policy. Reinterpreting live histories could otherwise bypass quota.
- Rate cleanup scans retained tenant histories under the lock. This is deliberately simple for the present deployment; measure contention before adopting a more complex expiry index. Cleanup is lazy, so idle expired entries remain until the next admission.
- The logger is process-wide, not tenant-specific. Multiple applications with different logging policies in one process are not independently configured.
- Fix one test-fixture issue: assigning logger.level restores the numeric value but leaves Python logging's severity cache stale. Restore it with setLevel instead.
- Additional checks cover custom token/message ceilings, invalid Unicode in direct and HTTP input, configuration consistency, provider timeout enforcement, and deadline expiry during local admission.

## Historical verification on the isolated proposal

These results describe the earlier isolated implementation, not the current
working tree or the test-only implementation above.

- Full existing and newly specified suite: **199 passed, 2 skipped**.
- Includes the four earlier focused checks and ten regression cases for the review findings.
- Ruff lint: passed.
- Fake-provider smoke script: passed.
- Real Uvicorn startup: liveness, readiness, and fake-provider chat passed.
- The two Linux signal/shutdown cases were skipped on Windows; run them in Linux CI before claiming shutdown behavior verified. No Docker image build or live-provider evaluation was performed for this proposal.
- At that time, no application source changes were applied to the working tree. Test changes have since been implemented as recorded above; the runtime changes remain pending.

## Corrections following review

- **Unicode errors:** request schemas validate UTF-8 for request IDs, messages, and body tenant metadata. Validation responses omit the rejected `input` field while preserving field locations, error types, messages, and validation context; this also avoids echoing private prompt text. Direct service policy retains its separate normalized-input validation.
- **One application configuration:** `create_app(service=...)` derives its settings from that service. Supplying different explicit settings raises ValueError before app construction rather than mixing HTTP limits, logging, and execution policies.
- **Deadline before provider work:** check remaining time after breaker acquisition and again after synchronous logging. Expiry releases a permit as ignored, records no provider call, and preserves any earlier actual attempt count and unknown outcome. A non-expired call receives the reduced remaining timeout.
- **Shutdown assertion:** accept either normal exit or `-signal.SIGTERM` after cleanup, since Uvicorn may re-raise the captured signal. Checks for short requests finishing, longer requests being cancelled, and released capacity remain; Windows still skips the real Linux signal cases.

## Proposed code, by destination

### `model_bridge/api/chat.py`

Builds the chat route using this application's configured validation limits and passes the actual HTTP body size to the service. The trusted tenant identity still comes from the existing dependency.

**Line context — current lines 1–7; proposed lines 1–7.**

```python
"""HTTP chat route; application execution is delegated to the service."""

from typing import Annotated
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from model_bridge.api.dependencies import (
    AuthenticatedTenant,
```

**Line context — current lines 9–34; proposed lines 9–46.**

```python
    get_chat_service,
)
from model_bridge.api.responses import to_http_response
from model_bridge.api.schemas import ChatResponse, create_chat_request_schema
from model_bridge.config.models import PlatformSettings
from model_bridge.application.chat_service import ChatService
from model_bridge.application.outcomes import ChatCommand


# Creates a router whose request schema uses the settings supplied to this application.
def create_chat_router(platform: PlatformSettings) -> APIRouter:
    router = APIRouter()
    request_schema = create_chat_request_schema(platform)

    # Replaces untrusted body metadata with tenant identity before executing the command.
    @router.post("/chat", response_model=ChatResponse)
    async def chat(
        request: request_schema,
        http_request: Request,
        tenant: Annotated[AuthenticatedTenant, Depends(get_authenticated_tenant)],
        service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> JSONResponse:
        command = ChatCommand(
            request_id=request.request_id,
            tenant_id=tenant.id,
            message=request.message,
            model_preference=request.model_preference,
            max_tokens=request.max_tokens,
            task_type=request.task_type,
        )
        return to_http_response(
            await service.handle(
                command,
                input_body_bytes=len(await http_request.body()),
            )
        )

    return router
```

### `model_bridge/api/responses.py`

Maps invalid input and oversized input outcomes to HTTP 422 and 413. A validation-error handler preserves field details while omitting rejected input, preventing invalid Unicode or private request text from being echoed into errors.

**Line context — current lines 1–5; proposed lines 1–8.**

```python
from dataclasses import asdict
from fastapi.responses import JSONResponse
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError

from model_bridge.application.outcomes import ChatOutcome
from model_bridge.api.schemas import ChatResponse
```

**Line context — current lines 11–16; proposed lines 14–21.**

```python
    "unknown": 202,
    "conflict": 409,
    "rate_limited": 429,
    "invalid_request": 422,
    "input_too_large": 413,
    "provider_rejected": 502,
    "failed": 503,
}
```

**Line context — current lines 31–33; proposed lines 36–50.**

```python
        content=ChatResponse(**asdict(outcome.response)).model_dump(mode="json"),
        headers=headers,
    )


# Retains field-validation details without echoing invalid strings or private input into errors.
async def validation_error_response(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    details = [
        {key: value for key, value in issue.items() if key != "input"}
        for issue in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(details)})
```

### `model_bridge/api/schemas.py`

Creates request schemas from the supplied platform settings and rejects invalid Unicode before it reaches application logs, metrics, or response fields. FastAPI retains field-validation responses when applications use different limits.

**Line context — current lines 1–6; proposed lines 1–6.**

```python
from typing import Literal
from pydantic import BaseModel, Field, create_model, field_validator
from model_bridge.config.models import PlatformSettings


# Validates incoming chat requests, including routing options and token limits.
```

**Line context — current lines 14–24; proposed lines 14–33.**

```python
    )
    message: str = Field(
        min_length=1,
    )
    model_preference: Literal["fast", "balanced"]
    max_tokens: int = Field(ge=1)
    task_type: Literal["simple", "complex", "high_risk"] | None = None

    # Rejects unpaired Unicode surrogates before values reach logs, metrics, or response fields.
    @field_validator("request_id", "message", "tenant_id")
    @classmethod
    def validate_utf8(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Input must contain valid UTF-8 characters") from exc
        return value


# Defines the API response fields for outcomes, generated content, routing, and usage.
```

**Line context — current lines 39–41; proposed lines 48–60.**

```python
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cache_hit: bool = False


# Builds HTTP validation from this application's settings while keeping service checks independent.
def create_chat_request_schema(platform: PlatformSettings) -> type[ChatRequest]:
    return create_model(
        "ChatRequest",
        __base__=ChatRequest,
        message=(str, Field(min_length=1, max_length=platform.max_message_characters)),
        max_tokens=(int, Field(ge=1, le=platform.max_output_tokens)),
    )
```

### `model_bridge/application/chat_service.py`

Checks input policy before rate admission or storage, then applies tenant rate limits and generation deadlines. It also uses the configured lease margin, supplies the fallback provider's timeout, and assigns severity to request outcome logs.

**Line context — current lines 16–22; proposed lines 16–23.**

```python
    ConcurrencyLimitExceeded,
    GenerationConcurrencyLimiter,
)
from model_bridge.execution.rate_limit import TenantAdmissionLimiter
from model_bridge.application.request_policy import validate_request_policy
from model_bridge.observability.logging import log_event
from model_bridge.observability.metrics import record_request, track_generation
from model_bridge.providers.contracts import (
```

**Line context — current lines 74–80; proposed lines 75–81.**

```python
    backup_provider: Provider | None
    primary_breaker: CircuitBreaker
    backup_breaker: CircuitBreaker | None
    chat_rate_limiter: TenantAdmissionLimiter
    generation_limiter: GenerationConcurrencyLimiter
    provider_name: str
    fallback_provider_name: str | None
```

**Line context — current lines 85–91; proposed lines 86–94.**

```python
    generate: Callable[..., Awaitable[GenerationResult]] = llm_call

    # Applies routing, admission, idempotency, generation and final persistence.
    async def handle(
        self, request: ChatCommand, *, input_body_bytes: int | None = None
    ) -> ChatOutcome:
        started_at = time.perf_counter()
        tenant_limits = self.settings.effective_tenant_limits(
            request.tenant_id,
```

**Line context — current lines 104–111; proposed lines 107–136.**

```python
            tenant=request.tenant_id,
            model_preference=model_preference,
        )
        violation = validate_request_policy(
            request,
            self.settings.platform,
            tenant_limits,
            input_body_bytes=input_body_bytes,
        )
        if violation is not None:
            return self._state_result(
                request,
                route,
                "failed",
                violation.detail,
                422 if violation.kind == "invalid_request" else 413,
                started_at,
                error_type=violation.kind,
            )
        generation_budget = min(
            self.deadline_seconds, tenant_limits.request_deadline_seconds
        )
        retry_after = self.chat_rate_limiter.try_acquire(
            request.tenant_id,
            limit=tenant_limits.rate_limit.requests,
            window_seconds=tenant_limits.rate_limit.window_seconds,
        )
        if retry_after is not None:
            response = self._state_result(
                request=request,
```

**Line context — current lines 124–130; proposed lines 149–155.**

```python
                request.tenant_id,
                request.request_id,
                asdict(request),
                generation_budget + self.settings.storage.lease_margin_seconds,
            )
        except IdempotencyConflictError as exc:
            # Return HTTP 409 because this request ID already represents different input.
```

**Line context — current lines 216–222; proposed lines 241–254.**

```python
                    route=route,
                    tenant_id=request.tenant_id,
                    timeout_seconds=self.timeout_seconds,
                    request_deadline_seconds=generation_budget,
                    fallback_timeout_seconds=(
                        self.settings.providers[
                            self.fallback_provider_name
                        ].attempt_timeout_seconds
                        if self.fallback_provider_name
                        else None
                    ),
                    max_attempts=self.max_attempts,
                    retry_base_delay_seconds=self.retry_delay_seconds,
                )
```

**Line context — current lines 465–470; proposed lines 497–505.**

```python
        )
        log_event(
            "request_finished",
            level="error"
            if status_code >= 500
            else ("warning" if status == "unknown" or status_code >= 400 else "info"),
            request_id=request.request_id,
            provider=provider_name,
            model=model,
```

**Line context — current lines 508–516; proposed lines 543–555.**

```python
            timed_out=timed_out,
            cache_hit=cache_hit,
        )
        kind = {
            409: "conflict",
            413: "input_too_large",
            422: "invalid_request",
            429: "rate_limited",
            502: "provider_rejected",
        }.get(status_code, status)
        return ChatOutcome(
            kind=kind,
            response=_state_response(request, status, route, detail, attempts),
```

### `model_bridge/application/outcomes.py`

Adds separate outcomes for invalid requests and oversized input. Application code can report these failures without depending on HTTP response objects.

**Line context — current lines 9–14; proposed lines 9–16.**

```python
    "unknown",
    "conflict",
    "rate_limited",
    "invalid_request",
    "input_too_large",
    "provider_rejected",
    "failed",
]
```

### `model_bridge/application/request_policy.py`

Introduces a small policy module that checks tokens, message length, and input bytes for both HTTP and direct service calls. Its normalized byte calculation excludes trusted tenant identity and leaves storage fingerprints unchanged.

**New file — proposed lines 1–81.**

```python
"""Request policy checks without HTTP, storage, or provider dependencies."""

import json
from dataclasses import dataclass
from typing import Literal

from model_bridge.application.outcomes import ChatCommand
from model_bridge.config.models import PlatformSettings, TenantLimits


# Describes a rejected policy without choosing an HTTP response.
@dataclass(frozen=True)
class PolicyViolation:
    kind: Literal["invalid_request", "input_too_large"]
    detail: str


# Measures caller-controlled input consistently for API and direct service calls.
def normalized_input_bytes(command: ChatCommand) -> int:
    payload = {
        "request_id": command.request_id,
        "message": command.message,
        "model_preference": command.model_preference,
        "max_tokens": command.max_tokens,
        "task_type": command.task_type,
    }
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


# Rejects excessive input before rate admission, persistence, or provider work.
def validate_request_policy(
    command: ChatCommand,
    platform: PlatformSettings,
    tenant: TenantLimits,
    *,
    input_body_bytes: int | None = None,
) -> PolicyViolation | None:
    if (
        not isinstance(command.request_id, str)
        or not 1 <= len(command.request_id) <= 128
        or not isinstance(command.message, str)
        or command.model_preference not in {"fast", "balanced"}
        or command.task_type not in {None, "simple", "complex", "high_risk"}
    ):
        return PolicyViolation("invalid_request", "Invalid caller input")
    token_limit = min(platform.max_output_tokens, tenant.max_output_tokens)
    if (
        isinstance(command.max_tokens, bool)
        or not isinstance(command.max_tokens, int)
        or not 1 <= command.max_tokens <= token_limit
    ):
        return PolicyViolation(
            "invalid_request", f"max_tokens must be between 1 and {token_limit}"
        )
    if not 1 <= len(command.message) <= platform.max_message_characters:
        return PolicyViolation(
            "invalid_request", "Message length exceeds the permitted range"
        )
    if input_body_bytes is not None and input_body_bytes < 0:
        raise ValueError("input_body_bytes cannot be negative")
    byte_limit = min(platform.max_input_bytes, tenant.max_input_bytes)
    try:
        normalized_size = normalized_input_bytes(command)
    except UnicodeEncodeError:
        return PolicyViolation(
            "invalid_request", "Input must contain valid UTF-8 characters"
        )
    if normalized_size > byte_limit or (
        input_body_bytes is not None and input_body_bytes > byte_limit
    ):
        return PolicyViolation(
            "input_too_large", f"Input exceeds the {byte_limit}-byte limit"
        )
    return None
```

### `model_bridge/execution/generation.py`

Rechecks the deadline after breaker acquisition and logging, releasing unused permits without counting a provider call if time expires. Each provider uses its own timeout within the shared deadline, attempt count, and retry-delay policy.

**Line context — current lines 1–5; proposed lines 1–7.**

```python
import asyncio
import random
import math
from model_bridge.providers.transport import run_with_attempt_timeout
import time
from dataclasses import replace

```

**Line context — current lines 17–22; proposed lines 19–25.**

```python
from model_bridge.providers.contracts import Provider
from model_bridge.providers.contracts import ProviderOutcomeUnknown
from model_bridge.providers.contracts import RetryableProviderError


async def complete_attempt(
    provider: Provider,
```

**Line context — current lines 30–42; proposed lines 33–48.**

```python
) -> GenerationResult:
    """Convert raw timeouts into the gateway's standard transient error."""
    try:
        return await run_with_attempt_timeout(
            provider.complete(
                request_id=request_id,
                attempt=attempt,
                message=message,
                model_preference=model_preference,
                max_tokens=max_tokens,
                timeout=timeout,
            ),
            timeout,
        )
    except asyncio.TimeoutError as exc:
        raise RetryableProviderError(
```

**Line context — current lines 45–50; proposed lines 51–58.**

```python
            outcome_unknown=True,
            failure_type="timeout",
        ) from exc


# Carries a rejected admission through the existing retryable-error API handler.
class CircuitUnavailableError(RetryableProviderError):
    def __init__(
```

**Line context — current lines 72–81; proposed lines 80–94.**

```python
def _counts_as_circuit_failure(error: RetryableProviderError) -> bool:
    if error.status_code == 429 or error.failure_type == "http_429":
        return False
    return error.failure_type in {
        "timeout",
        "connection_error",
    } or error.status_code in {408, 500, 502, 503, 504}


# Signals that local admission used the remaining time before any provider call began.
class _AttemptDeadlineExpired(Exception):
    pass


# Completes an admitted call and releases its permit on every exit, including cancellation.
```

**Line context — current lines 93–98; proposed lines 106–112.**

```python
    timeout: float,
    route: str,
    tenant_id: str,
    deadline: float,
) -> GenerationResult:
    outcome: CallOutcome = "ignored"
    try:
```

**Line context — current lines 115–120; proposed lines 129–138.**

```python
            attempt=attempt,
            timeout_ms=round(timeout * 1000),
        )
        # Admission and synchronous logging can consume the remaining generation budget.
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _AttemptDeadlineExpired
        result = await complete_attempt(
            provider,
            request_id=request_id,
```

**Line context — current lines 122–128; proposed lines 140–146.**

```python
            message=message,
            model_preference=model_preference,
            max_tokens=max_tokens,
            timeout=min(timeout, remaining),
        )
        outcome = "success"
        return result
```

**Line context — current lines 160–169; proposed lines 178–198.**

```python
    route: str = "default",
    tenant_id: str = "default",
    timeout_seconds: float = 30,
    fallback_timeout_seconds: float | None = None,
    request_deadline_seconds: float = 45,
    max_attempts: int = 3,
    retry_base_delay_seconds: float = 0.25,
) -> GenerationResult:
    fallback_timeout = (
        timeout_seconds
        if fallback_timeout_seconds is None
        else fallback_timeout_seconds
    )
    for budget in (timeout_seconds, fallback_timeout, request_deadline_seconds):
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("Timeouts and deadlines must be positive and finite")
    if not math.isfinite(retry_base_delay_seconds) or retry_base_delay_seconds < 0:
        raise ValueError("Retry delay must be nonnegative and finite")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if fallback_provider is not None and not fallback_provider_name:
```

**Line context — current lines 187–192; proposed lines 216–222.**

```python
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request_deadline_seconds
    last_error: Exception | None = None
    active_timeout = timeout_seconds
    active_provider = provider
    active_provider_name = provider_name
    active_breaker = circuit_breaker
```

**Line context — current lines 196–202; proposed lines 226–232.**

```python

    # Preserves completed attempt count and earlier uncertainty when no retry time remains.
    def deadline_error(attempts: int) -> RetryableProviderError:

        return RetryableProviderError(
            "The request deadline leaves no time for another attempt",
            attempts=attempts,
```

**Line context — current lines 206–212; proposed lines 236–247.**

```python

    # Selects fallback once without consuming a provider-call attempt.
    def select_fallback(error_type: str, status_code: int) -> bool:
        nonlocal \
            active_provider, \
            active_provider_name, \
            active_breaker, \
            fallback_used, \
            active_timeout
        if fallback_provider is None or fallback_used:
            return False
        record_fallback(
```

**Line context — current lines 230–235; proposed lines 265–271.**

```python
            error_type=error_type,
            status_code=status_code,
        )
        active_timeout = fallback_timeout
        active_provider = fallback_provider
        active_provider_name = fallback_provider_name
        active_breaker = fallback_circuit_breaker
```

**Line context — current lines 254–262; proposed lines 290–297.**

```python
                    retry_after=exc.retry_after,
                    attempts=attempt,
                )
                if fallback_circuit_breaker is not active_breaker and select_fallback(
                    "circuit_open", 503
                ):
                    continue
                raise CircuitUnavailableError(
```

**Line context — current lines 267–274; proposed lines 302–324.**

```python
                    model=_metric_model(active_provider_name, model_preference),
                ) from exc

        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            if active_breaker is not None and permit is not None:
                transition = active_breaker.finish(permit, "ignored")
                if transition is not None:
                    previous, current = transition
                    log_event(
                        "circuit_state_changed",
                        request_id=request_id,
                        provider=active_provider_name,
                        previous_state=previous,
                        state=current,
                    )
            raise deadline_error(attempt) from last_error

        attempt += 1
        attempt_timeout = min(active_timeout, remaining_seconds)
        attempt_started = time.perf_counter()
        model_name = _metric_model(active_provider_name, model_preference)
        try:
```

**Line context — current lines 285–291; proposed lines 335–346.**

```python
                timeout=attempt_timeout,
                route=route,
                tenant_id=tenant_id,
                deadline=deadline,
            )
        except _AttemptDeadlineExpired:
            # The permit was released as ignored; preserve only actual calls and earlier uncertainty.
            attempt -= 1
            raise deadline_error(attempt) from last_error
        except ProviderOutcomeUnknown as exc:
            # Stop further attempts because completion is explicitly uncertain; preserve the attempt count.
            _record_attempt(
```

**Line context — current lines 315–323; proposed lines 370–378.**

```python
                tenant=tenant_id,
                status_code=exc.status_code,
            )

            last_error = exc
            any_outcome_unknown = any_outcome_unknown or exc.outcome_unknown
            attempts_exhausted = attempt >= max_attempts
            deadline_expired = loop.time() >= deadline
            if deadline_expired:
```

**Line context — current lines 369–385; proposed lines 424–440.**

```python
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
```

**Line context — current lines 436–441; proposed lines 491–497.**

```python
    )
    log_event(
        "provider_attempt_finished",
        level="info" if status == "success" else "warning",
        request_id=request_id,
        provider=provider_name,
        attempt=attempt,
```

### `model_bridge/execution/rate_limit.py`

Adds a tenant-aware limiter that checks and spends tenant and platform quotas together under one lock. It expires old histories, calculates Retry-After, and avoids spending quota or creating tenant histories for rejected requests.

**Line context — current lines 1–6; proposed lines 1–9.**

```python
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from threading import Lock


```

**Line context — current lines 29–38; proposed lines 32–130.**

```python
                self._timestamps.popleft()

            if len(self._timestamps) >= self.limit:
                retry_after = self._timestamps[0] + self.window_seconds - now
                return max(1, math.ceil(retry_after))

            self._timestamps.append(now)
            return None


# Defines the admission boundary used by the application service.
class TenantAdmissionLimiter(Protocol):
    # Returns a retry delay unless both tenant and platform quotas admit the request.
    def try_acquire(
        self, tenant_id: str, *, limit: int, window_seconds: float
    ) -> int | None: ...


# Keeps one tenant's admission history and the window used to expire it.
@dataclass(slots=True)
class _TenantWindow:
    limit: int
    seconds: float
    timestamps: deque[float] = field(default_factory=deque)


# Enforces platform and tenant windows atomically within one application process.
class TenantRateLimiter:
    # Accepts a monotonic clock so window calculations can be verified deterministically.
    def __init__(
        self,
        platform_limit: int,
        platform_window_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._validate(platform_limit, platform_window_seconds)
        self._platform_limit = platform_limit
        self._platform_window = platform_window_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._timestamps: deque[float] = deque()
        self._tenants: dict[str, _TenantWindow] = {}
        self._lock = Lock()

    # Validates quota arguments supplied outside the settings loader.
    @staticmethod
    def _validate(limit: int, seconds: float) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Rate limit must be a positive integer")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Rate-limit window must be positive and finite")

    # Expires admissions exactly at the end of their rolling window.
    @staticmethod
    def _expire(timestamps: deque[float], cutoff: float) -> None:
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    # Supplies a read-only snapshot of tenant histories retained in memory.
    @property
    def active_tenant_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._tenants)

    # Checks both windows before spending either quota; rejected requests allocate no tenant history.
    def try_acquire(
        self, tenant_id: str, *, limit: int, window_seconds: float
    ) -> int | None:
        self._validate(limit, window_seconds)
        with self._lock:
            now = self._clock()
            self._expire(self._timestamps, now - self._platform_window)
            for identity, history in list(self._tenants.items()):
                self._expire(history.timestamps, now - history.seconds)
                if not history.timestamps:
                    del self._tenants[identity]

            history = self._tenants.get(tenant_id)
            if history is not None and (
                history.limit != limit or history.seconds != window_seconds
            ):
                # Changing a live window would erase or reinterpret already spent quota.
                raise ValueError(
                    "Tenant rate policy changed while admissions remain active; restart with new settings"
                )
            waits = []
            if len(self._timestamps) >= self._platform_limit:
                waits.append(self._timestamps[0] + self._platform_window - now)
            if history is not None and len(history.timestamps) >= limit:
                waits.append(history.timestamps[0] + window_seconds - now)
            if waits:
                return max(1, math.ceil(max(waits)))

            if history is None:
                history = _TenantWindow(limit, window_seconds)
                self._tenants[tenant_id] = history
            self._timestamps.append(now)
            history.timestamps.append(now)
            return None
```

### `model_bridge/main.py`

Uses the injected service's settings when none are supplied and rejects explicit configuration mismatches. It wires the configured policies, safe validation errors, and server entry point with configurable shutdown grace.

**Line context — current lines 1–16; proposed lines 1–19.**

```python
"""Application composition: construct collaborators and register HTTP adapters."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from model_bridge.api.responses import validation_error_response
from prometheus_client import make_asgi_app
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from model_bridge.api.chat import create_chat_router
from model_bridge.api.health import router as health_router
from model_bridge.application.chat_service import ChatService
from model_bridge.config.loader import PROJECT_ROOT, SETTINGS
from model_bridge.config.models import Settings
from model_bridge.execution.circuit_breaker import CircuitBreaker
from model_bridge.execution.concurrency_limit import GenerationConcurrencyLimiter
from model_bridge.execution.rate_limit import TenantRateLimiter
from model_bridge.observability.logging import configure_logging
from model_bridge.providers.factory import create_provider
from model_bridge.storage.request_store import RequestStore

```

**Line context — current lines 36–49; proposed lines 39–55.**

```python
    }
    return ChatService(
        settings=settings,
        request_store=RequestStore(
            settings.storage.resolved_path(PROJECT_ROOT),
            busy_timeout_seconds=settings.storage.busy_timeout_seconds,
        ),
        primary_provider=create_provider(primary, settings=settings),
        backup_provider=create_provider(fallback, settings=settings)
        if fallback
        else None,
        primary_breaker=breakers[primary],
        backup_breaker=breakers.get(fallback),
        chat_rate_limiter=TenantRateLimiter(
            settings.platform.rate_limit.requests,
            settings.platform.rate_limit.window_seconds,
        ),
```

**Line context — current lines 61–71; proposed lines 67–83.**

```python

# Wires a fresh service or an explicitly supplied service into the HTTP application.
def create_app(
    settings: Settings | None = None,
    *,
    service: ChatService | None = None,
) -> FastAPI:
    if settings is None:
        settings = service.settings if service is not None else SETTINGS
    elif service is not None and settings != service.settings:
        raise ValueError("Application and service settings must match")
    configure_logging(settings.platform.logging)
    application = FastAPI()
    application.add_exception_handler(RequestValidationError, validation_error_response)
    application.state.chat_service = (
        service if service is not None else create_chat_service(settings)
    )
```

**Line context — current lines 75–83; proposed lines 87–113.**

```python
    )
    if settings.platform.metrics.enabled:
        application.mount("/metrics", make_asgi_app())
    application.include_router(create_chat_router(settings.platform))
    application.include_router(health_router)
    return application


# Starts one worker with the configured drain period; provider and limiter state are process-local.
def run_server(
    settings: Settings = SETTINGS, *, host: str = "0.0.0.0", port: int = 8000
) -> None:
    import uvicorn

    uvicorn.run(
        create_app(settings=settings),
        host=host,
        port=port,
        workers=1,
        timeout_graceful_shutdown=settings.platform.shutdown_grace_seconds,
    )


if __name__ == "__main__":
    run_server()
else:
    app = create_app()
```

### `model_bridge/observability/logging.py`

Applies the logging enabled switch and minimum severity without adding duplicate handlers. Events retain structured JSON and UTC timestamps, while callers remain responsible for excluding private content.

**Line context — current lines 3–34; proposed lines 3–46.**

```python
from datetime import datetime, timezone
from typing import Any

from model_bridge.config.models import LoggingSettings

logger = logging.getLogger("model_bridge")
logger.propagate = False
LEVELS = {
    name: getattr(logging, name.upper())
    for name in ("debug", "info", "warning", "error", "critical")
}


# Applies process-wide application logging settings without duplicating handlers.
def configure_logging(settings: LoggingSettings) -> None:
    logger.disabled = not settings.enabled
    logger.setLevel(LEVELS[settings.level])
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


# Emits only operational metadata at the selected severity, with an explicit UTC timestamp.
def log_event(event: str, *, level: str = "info", **fields: Any) -> None:
    if level not in LEVELS:
        raise ValueError(f"Unknown event severity: {level}")
    severity = LEVELS[level]
    if not logger.isEnabledFor(severity):
        return
    timestamp = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    logger.log(
        severity,
        json.dumps(
            {**fields, "event": event, "timestamp": timestamp, "level": level},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
    )
```

### `model_bridge/storage/request_store.py`

Validates and applies the configured SQLite busy timeout to normal database connections. Readiness retains its separate bounded read timeout; the remaining edits improve formatting without changing storage behavior.

**Line context — current lines 1–3; proposed lines 1–4.**

```python
import math
import hashlib
import json
import sqlite3
```

**Line context — current lines 26–32; proposed lines 27–38.**

```python
# Uses SQLite to claim request IDs, detect duplicates, track processing leases, and save outcomes.
class RequestStore:
    # Prepares the database directory, enables WAL journaling, and creates request storage if needed.
    def __init__(
        self, database_path: str | Path, *, busy_timeout_seconds: float = 5
    ) -> None:
        if not math.isfinite(busy_timeout_seconds) or busy_timeout_seconds <= 0:
            raise ValueError("SQLite busy timeout must be positive and finite")
        self.busy_timeout_seconds = busy_timeout_seconds
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
```

**Line context — current lines 50–70; proposed lines 56–78.**

```python
                """
            )
            connection.commit()

    # Checks existing storage without creating a missing database or table.
    def check_readable(self) -> None:
        database_uri = self.database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True, timeout=1)
        try:
            connection.execute("SELECT request_id FROM requests LIMIT 1").fetchone()
        finally:
            # Release the connection even when the query fails.
            connection.close()

    # Yields a SQLite connection with named columns and closes it when the caller exits.
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path, timeout=self.busy_timeout_seconds
        )
        connection.row_factory = sqlite3.Row
        try:
            yield connection
```

**Line context — current lines 171–177; proposed lines 179–187.**

```python
            )

    # Saves the successful response and its attempt count for future replay.
    def mark_success(
        self, tenant_id: str, request_id: str, response: dict[str, Any]
    ) -> None:
        self._finish(
            tenant_id,
            request_id,
```

**Line context — current lines 182–188; proposed lines 192–200.**

```python
        )

    # Saves an uncertain outcome so duplicate requests do not restart generation.
    def mark_unknown(
        self, tenant_id: str, request_id: str, detail: str, attempts: int
    ) -> None:
        self._finish(
            tenant_id,
            request_id,
```

**Line context — current lines 193–199; proposed lines 205–213.**

```python
        )

    # Saves a definite failure and its attempt count.
    def mark_failed(
        self, tenant_id: str, request_id: str, detail: str, attempts: int
    ) -> None:
        self._finish(
            tenant_id,
            request_id,
```

**Line context — current lines 202–207; proposed lines 216–222.**

```python
            detail=detail,
            attempts=attempts,
        )

    # Preserves request identity while allowing retry after zero provider calls.
    def mark_retryable(self, tenant_id: str, request_id: str, detail: str) -> None:
        self._finish(
```

**Line context — current lines 212–217; proposed lines 227–233.**

```python
            detail=detail,
            attempts=0,
        )

    # Updates an in-progress request and rejects missing or already-finalized records.
    def _finish(
        self,
```

**Line context — current lines 229–235; proposed lines 245–259.**

```python
                SET status = ?, response_json = ?, detail = ?, attempts = ?, updated_at = ?
                WHERE tenant_id = ? AND request_id = ? AND status = 'in_progress'
                """,
                (
                    status,
                    response_json,
                    detail,
                    attempts,
                    time.time(),
                    tenant_id,
                    request_id,
                ),
            )
            connection.commit()
            if cursor.rowcount != 1:
```


### `Dockerfile`

Starts the container through the new application server entry point. Shutdown grace is therefore read from configuration rather than fixed in the Docker command.

**Line context — current lines 28–33; proposed lines 28–31.**

```dockerfile
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/ready', timeout=2).close()"]

CMD ["python", "-m", "model_bridge.main"]
```

