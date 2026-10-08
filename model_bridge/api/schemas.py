from typing import Literal
from pydantic import BaseModel, Field, create_model, field_validator
from model_bridge.config.models import PlatformSettings

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


# Builds HTTP validation from this application's settings while keeping service checks independent.
def create_chat_request_schema(platform: PlatformSettings) -> type[ChatRequest]:
    return create_model(
        "ChatRequest",
        __base__=ChatRequest,
        message=(str, Field(min_length=1, max_length=platform.max_message_characters)),
        max_tokens=(int, Field(ge=1, le=platform.max_output_tokens)),
    )
