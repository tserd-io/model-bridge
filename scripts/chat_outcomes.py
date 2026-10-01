from dataclasses import dataclass
from typing import Literal

from scripts.schemas import ChatResponse


OutcomeKind = Literal[
    "success",
    "in_progress",
    "unknown",
    "conflict",
    "rate_limited",
    "provider_rejected",
    "failed",
]


# Carries the application result and metadata needed by the API adapter.
@dataclass(frozen=True)
class ChatOutcome:
    kind: OutcomeKind
    response: ChatResponse
    retry_after: int | None = None
    replayed: bool = False
    error_type: str = "none"
    timed_out: bool = False