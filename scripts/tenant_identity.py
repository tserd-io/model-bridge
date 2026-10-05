from dataclasses import dataclass

from fastapi import HTTPException, Request


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