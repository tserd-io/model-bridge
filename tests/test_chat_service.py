"""The application workflow runs directly without HTTP clients or schemas."""

import asyncio
from model_bridge.application.outcomes import ChatCommand, ChatOutcome
from model_bridge.execution.rate_limit import SlidingWindowRateLimiter


# Verifies direct service execution isolates tenants sharing an ID and replays saved results.
def test_service_handles_tenant_scoped_commands_without_http(isolated_chat_service):
    service = isolated_chat_service
    service.chat_rate_limiter = SlidingWindowRateLimiter(100, 60)
    command_a = ChatCommand("shared-id", "tenant-a", "first", "fast", 10)
    command_b = ChatCommand("shared-id", "tenant-b", "second", "fast", 10)

    async def scenario():
        first = await service.handle(command_a)
        second = await service.handle(command_b)
        replay = await service.handle(command_a)
        assert isinstance(first, ChatOutcome)
        assert first.kind == second.kind == replay.kind == "success"
        assert first.response.content == "Fake response: first"
        assert second.response.content == "Fake response: second"
        assert second.response.tenant_id == "tenant-b"
        assert replay.replayed is True
        assert replay.response.cache_hit is True
        assert replay.response.content == first.response.content

    asyncio.run(scenario())
