from model_bridge.providers.contracts import GenerationResult


# Returns predictable text without a network call for isolated development and evaluation.
class FakeProvider:
    # Returns deterministic text and model identity without contacting an external provider.
    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        return GenerationResult(
            content=f"Fake response: {message}",
            model=f"fake-{model_preference}",
            provider="fake",
        )
