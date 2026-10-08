from dataclasses import asdict
from fastapi.responses import JSONResponse
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError

from model_bridge.application.outcomes import ChatOutcome
from model_bridge.api.schemas import ChatResponse



HTTP_STATUS = {
    "success": 200,
    "in_progress": 202,
    "unknown": 202,
    "conflict": 409,
    "rate_limited": 429,
    "invalid_request": 422,
    "input_too_large": 413,
    "provider_rejected": 502,
    "failed": 503,
}



# Converts an application outcome into the public HTTP response.
def to_http_response(outcome: ChatOutcome) -> JSONResponse:
    headers = {}

    if outcome.retry_after is not None:
        headers["Retry-After"] = str(outcome.retry_after)

    if outcome.replayed:
        headers["X-Idempotent-Replay"] = "true"

    return JSONResponse(
        status_code=HTTP_STATUS[outcome.kind],
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
