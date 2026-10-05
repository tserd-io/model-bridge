from dataclasses import dataclass

from fastapi import HTTPException, Request
from model_bridge.application.chat_service import ChatService


# Represents identity established by a trusted authenticator.
@dataclass(frozen=True)
class AuthenticatedTenant:
    id: str


# Requires authentication middleware to establish the tenant identity.
async def get_authenticated_tenant(
    request: Request,
) -> AuthenticatedTenant:
    tenant = getattr(request.state, "authenticated_tenant", None)

    if not isinstance(tenant, AuthenticatedTenant) or not tenant.id:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )

    return tenant


# Retrieves the application service wired by the app factory for this request.
def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service
