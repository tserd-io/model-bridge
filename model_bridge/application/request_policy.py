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
