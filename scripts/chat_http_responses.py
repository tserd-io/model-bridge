from fastapi.responses import JSONResponse

from scripts.chat_outcomes import ChatOutcome


HTTP_STATUS = {
    "success": 200,
    "in_progress": 202,
    "unknown": 202,
    "conflict": 409,
    "rate_limited": 429,
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
        content=outcome.response.model_dump(mode="json"),
        headers=headers,
    )