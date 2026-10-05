"""Construct configured adapters without importing the application."""

from model_bridge.config.loader import SETTINGS
from model_bridge.config.models import Settings
from model_bridge.providers.contracts import Provider
from model_bridge.providers.fake import FakeProvider
from model_bridge.providers.ollama import OllamaProvider
from model_bridge.providers.openai import OpenAIProvider


# Uses the selected configured backend and rejects missing or unsupported entries.
def create_provider(
    provider_name: str | None = None,
    *,
    settings: Settings = SETTINGS,
) -> Provider:
    selected = (provider_name or settings.primary_provider).lower()
    configured = settings.providers.get(selected)
    if configured is None:
        raise ValueError(f"Unsupported LLM provider: {selected}")
    if configured.type == "ollama":
        return OllamaProvider(host=configured.endpoint, models=dict(configured.models))
    if configured.type == "openai":
        return OpenAIProvider(models=dict(configured.models))
    if configured.type == "fake":
        return FakeProvider()
    raise ValueError(f"Unsupported LLM provider: {selected}")
