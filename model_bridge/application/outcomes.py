"""Application inputs and outcomes without HTTP or framework dependencies."""

from dataclasses import dataclass
from typing import Literal

OutcomeKind = Literal[
    "success",
    "in_progress",
    "unknown",
    "conflict",
    "rate_limited",
    "invalid_request",
    "input_too_large",
    "provider_rejected",
    "failed",
]

ResponseStatus = Literal["success", "in_progress", "unknown", "failed"]


# Carries validated input and the trusted tenant identity into the application.
@dataclass(frozen=True)
class ChatCommand:
    request_id: str
    tenant_id: str
    message: str
    model_preference: str
    max_tokens: int
    task_type: str | None = None


# Carries a stored or generated result independently of the HTTP response schema.
@dataclass(frozen=True)
class ChatResult:
    request_id: str
    status: ResponseStatus
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


# Supplies semantic outcome and delivery metadata for the HTTP adapter.
@dataclass(frozen=True)
class ChatOutcome:
    kind: OutcomeKind
    response: ChatResult
    retry_after: int | None = None
    replayed: bool = False
    error_type: str = "none"
    timed_out: bool = False
