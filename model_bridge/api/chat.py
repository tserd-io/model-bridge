"""HTTP chat route; application execution is delegated to the service."""

"""HTTP chat route; application execution is delegated to the service."""

from typing import Annotated
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from model_bridge.api.dependencies import (
    AuthenticatedTenant,
    get_authenticated_tenant,
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
