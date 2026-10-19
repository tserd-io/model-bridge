"""Controlled provider responses leave the real generation runner under test."""

import asyncio
from collections import deque
from model_bridge.providers.contracts import GenerationResult


# Returns scripted results/errors, followed by deterministic successful responses.
class ScriptedProvider:
    def __init__(self, *outcomes, name="fake"):
        self.outcomes = deque(outcomes)
        self.name = name
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(dict(kwargs))
        outcome = (
            self.outcomes.popleft()
            if self.outcomes
            else GenerationResult(
                content=f"Fake response: {kwargs['message']}",
                model=f"{self.name}-{kwargs['model_preference']}",
                provider=self.name,
                input_tokens=10,
                output_tokens=5,
                estimated_cost_usd=0.001,
            )
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# Holds a genuine provider call until the test explicitly releases it.
class BlockingProvider(ScriptedProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().complete(**kwargs)
