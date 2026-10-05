"""HTTP chat route; application execution is delegated to the service."""

from typing import Annotated
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from model_bridge.api.dependencies import (
    AuthenticatedTenant,
    get_authenticated_tenant,
    get_chat_service,
)
from model_bridge.api.responses import to_http_response
from model_bridge.api.schemas import ChatRequest, ChatResponse
from model_bridge.application.chat_service import ChatService
from model_bridge.application.outcomes import ChatCommand

router = APIRouter()


# Replaces untrusted body metadata with tenant identity before executing the command.
@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
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
    return to_http_response(await service.handle(command))
