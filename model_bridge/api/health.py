"""HTTP health probes, using the storage dependency owned by the service."""

import asyncio
import sqlite3
from typing import Annotated
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from model_bridge.api.dependencies import get_chat_service
from model_bridge.application.chat_service import ChatService

router = APIRouter()


# Reports responsiveness without inspecting storage or contacting a provider.
@router.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


# Reads existing storage and returns a bounded readiness response without error details.
@router.get("/health/ready", tags=["health"])
async def readiness(
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> JSONResponse:
    try:
        await asyncio.to_thread(service.request_store.check_readable)
    except sqlite3.Error:
        # Database failure makes readiness fail while leaving liveness independent.
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": {"database": "failed"}},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ready", "checks": {"database": "ok"}},
    )
