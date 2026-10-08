"""Application composition: construct collaborators and register HTTP adapters."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from model_bridge.api.responses import validation_error_response
from prometheus_client import make_asgi_app
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from model_bridge.api.chat import create_chat_router
from model_bridge.api.health import router as health_router
from model_bridge.application.chat_service import ChatService
from model_bridge.config.loader import PROJECT_ROOT, SETTINGS
from model_bridge.config.models import Settings
from model_bridge.execution.circuit_breaker import CircuitBreaker
from model_bridge.execution.concurrency_limit import GenerationConcurrencyLimiter
from model_bridge.execution.rate_limit import TenantRateLimiter
from model_bridge.observability.logging import configure_logging
from model_bridge.providers.factory import create_provider
from model_bridge.storage.request_store import RequestStore




# Constructs one process-local set of chat dependencies from validated settings.
def create_chat_service(settings: Settings = SETTINGS) -> ChatService:
    primary = settings.primary_provider
    fallback = settings.fallback_provider
    provider_settings = settings.providers[primary]
    # Share a breaker for each configured provider name.
    # Each breaker uses that provider's own threshold and cooldown.
    breakers = {
        name: CircuitBreaker(
            failure_threshold=(
                settings.providers[name].circuit_breaker_failure_threshold
            ),
            cooldown_seconds=(
                settings.providers[name].circuit_breaker_cooldown_seconds
            ),
        )
        for name in {primary, fallback}
        if name is not None
        }
    return ChatService(
        settings=settings,
        request_store=RequestStore(
            settings.storage.resolved_path(PROJECT_ROOT),
            busy_timeout_seconds=settings.storage.busy_timeout_seconds,
        ),
        primary_provider=create_provider(primary, settings=settings),
        backup_provider=create_provider(fallback, settings=settings)
        if fallback
        else None,
        primary_breaker=breakers[primary],
        backup_breaker=breakers.get(fallback),
        chat_rate_limiter=TenantRateLimiter(
            settings.platform.rate_limit.requests,
            settings.platform.rate_limit.window_seconds,
        ),

        generation_limiter=GenerationConcurrencyLimiter(
            settings.platform.max_concurrent_jobs_per_instance,
        ),
        provider_name=primary,
        fallback_provider_name=fallback,
        timeout_seconds=provider_settings.attempt_timeout_seconds,
        deadline_seconds=settings.platform.request_deadline_seconds,
        max_attempts=provider_settings.max_attempts,
        retry_delay_seconds=provider_settings.retry_base_delay_seconds,
    )



# Wires a fresh service or an explicitly supplied service into the HTTP application.
def create_app(
    settings: Settings | None = None,
    *,
    service: ChatService | None = None,
) -> FastAPI:
    if settings is None:
        settings = service.settings if service is not None else SETTINGS
    elif service is not None and settings != service.settings:
        raise ValueError("Application and service settings must match")
    configure_logging(settings.platform.logging)
    application = FastAPI()
    application.add_exception_handler(RequestValidationError, validation_error_response)
    application.state.chat_service = (
        service if service is not None else create_chat_service(settings)
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=settings.platform.max_input_bytes,
    )
    if settings.platform.metrics.enabled:
        application.mount("/metrics", make_asgi_app())
    application.include_router(create_chat_router(settings.platform))
    application.include_router(health_router)
    return application

# Starts one worker with the configured drain period; provider and limiter state are process-local.
def run_server(
    settings: Settings = SETTINGS, *, host: str = "0.0.0.0", port: int = 8000
) -> None:
    import uvicorn

    uvicorn.run(
        create_app(settings=settings),
        host=host,
        port=port,
        workers=1,
        timeout_graceful_shutdown=settings.platform.shutdown_grace_seconds,
    )


if __name__ == "__main__":
    run_server()
else:
    app = create_app()
