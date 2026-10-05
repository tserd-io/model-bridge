from typing import Literal
from pydantic import BaseModel, Field
from scripts.load_settings import PLATFORM_SETTINGS


# Validates incoming chat requests, including routing options and token limits.
class ChatRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    message: str = Field(
        min_length=1,
        max_length=PLATFORM_SETTINGS.max_message_characters,
    )
    model_preference: Literal["fast", "balanced"]
    max_tokens: int = Field(ge=1, le=8192)
    task_type: Literal["simple", "complex", "high_risk"] | None = None


# Defines the API response fields for outcomes, generated content, routing, and usage.
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