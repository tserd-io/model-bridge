"""Validated JSON settings objects with compatibility exports for existing code.

JSON must validate independently. Existing environment variables override it;
final validation rejects invalid values or tenant policies above platform ceilings.
No clients or database connections are opened here.
"""
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Final

from model_bridge.config.models import ProviderSettings
from model_bridge.config.models import Settings

CONFIG_PATH: Final = Path(__file__).with_name("config.json")
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


def load_settings(config_path: str | Path = CONFIG_PATH, *, environ=None) -> Settings:
    """Load a snapshot; pass environ={} to ignore process environment in tests.

    Relative storage paths resolve against PROJECT_ROOT. Settings changes require
    explicit reload or restart. Credentials remain outside this configuration.
    """
    env = os.environ if environ is None else environ
    with Path(config_path).open(encoding="utf-8") as source:
        data = Settings.from_config_dict(json.load(source)).model_dump()
    if "LLM_PROVIDER" in env:
        data["primary_provider"] = env["LLM_PROVIDER"].strip().lower()
    if "LLM_FALLBACK_PROVIDER" in env:
        data["fallback_provider"] = env["LLM_FALLBACK_PROVIDER"].strip().lower() or None
    for name in (data["primary_provider"], data["fallback_provider"]):
        if name == "fake" and name not in data["providers"]:
            data["providers"][name] = ProviderSettings(type="fake").model_dump()
    overrides = {
        "LLM_TIMEOUT_SECONDS": "attempt_timeout_seconds",
        "MAX_LLM_ATTEMPTS": "max_attempts",
        "RETRY_BASE_DELAY_SECONDS": "retry_base_delay_seconds",
        "CIRCUIT_BREAKER_FAILURE_THRESHOLD": "circuit_breaker_failure_threshold",
        "CIRCUIT_BREAKER_COOLDOWN_SECONDS": "circuit_breaker_cooldown_seconds",
    }
    for name, provider in data["providers"].items():
        for variable, field in overrides.items():
            if variable in env:
                provider[field] = env[variable]
        if provider["type"] == "ollama" and "OLLAMA_HOST" in env:
            provider["endpoint"] = env["OLLAMA_HOST"]
        for preference in provider["models"]:
            variable = f"{name.upper()}_MODEL_{preference.upper()}"
            if variable in env:
                provider["models"][preference] = env[variable]
        if provider["type"] == "openai" and "MODEL_PRICING_USD_PER_MILLION_TOKENS" in env:
            provider["pricing_usd_per_million_tokens"] = json.loads(env["MODEL_PRICING_USD_PER_MILLION_TOKENS"])
    if "REQUEST_DEADLINE_SECONDS" in env:
        data["platform"]["request_deadline_seconds"] = env["REQUEST_DEADLINE_SECONDS"]
    for variable, field in {"RATE_LIMIT_REQUESTS": "requests", "RATE_LIMIT_WINDOW_SECONDS": "window_seconds"}.items():
        if variable in env:
            data["platform"]["rate_limit"][field] = env[variable]
    if "IDEMPOTENCY_DB_PATH" in env:
        data["storage"]["path"] = env["IDEMPOTENCY_DB_PATH"]
    settings = Settings.model_validate(data)
    data["storage"]["path"] = settings.storage.resolved_path(PROJECT_ROOT)
    return Settings.model_validate(data)


# Preferred object exports. Frozen models are shallow: nested mappings must be
# treated as read-only. Mapping views additionally prevent top-level mutations.
SETTINGS: Final = load_settings()
PLATFORM_SETTINGS: Final = SETTINGS.platform
TENANT_DEFAULTS: Final = SETTINGS.tenant_defaults
TENANT_OVERRIDES: Final = MappingProxyType(SETTINGS.tenant_overrides)
PROVIDER_SETTINGS: Final = MappingProxyType(SETTINGS.providers)
STORAGE_SETTINGS: Final = SETTINGS.storage
PRIMARY_PROVIDER_SETTINGS: Final = PROVIDER_SETTINGS[SETTINGS.primary_provider]
FALLBACK_PROVIDER_SETTINGS: Final = (
    PROVIDER_SETTINGS[SETTINGS.fallback_provider] if SETTINGS.fallback_provider else None
)

# Legacy exports allow gradual migration to explicit settings injection.
LLM_PROVIDER = SETTINGS.primary_provider
FALLBACK_PROVIDER = SETTINGS.fallback_provider
OLLAMA_HOST = next((p.endpoint for p in PROVIDER_SETTINGS.values() if p.type == "ollama"), "http://localhost:11434")
IDEMPOTENCY_DB_PATH = str(STORAGE_SETTINGS.path)
DEFAULT_LLM_TIMEOUT_SECONDS = PRIMARY_PROVIDER_SETTINGS.attempt_timeout_seconds
REQUEST_DEADLINE_SECONDS = PLATFORM_SETTINGS.request_deadline_seconds
MAX_LLM_ATTEMPTS = PRIMARY_PROVIDER_SETTINGS.max_attempts
RETRY_BASE_DELAY_SECONDS = PRIMARY_PROVIDER_SETTINGS.retry_base_delay_seconds
CIRCUIT_BREAKER_FAILURE_THRESHOLD = PRIMARY_PROVIDER_SETTINGS.circuit_breaker_failure_threshold
CIRCUIT_BREAKER_COOLDOWN_SECONDS = PRIMARY_PROVIDER_SETTINGS.circuit_breaker_cooldown_seconds
RATE_LIMIT_REQUESTS = PLATFORM_SETTINGS.rate_limit.requests
RATE_LIMIT_WINDOW_SECONDS = PLATFORM_SETTINGS.rate_limit.window_seconds
MODEL_PREFERENCES = {
    preference: {name: p.models[preference] for name, p in PROVIDER_SETTINGS.items() if preference in p.models}
    for preference in {key for p in PROVIDER_SETTINGS.values() for key in p.models}
}
MODEL_PRICING_USD_PER_MILLION_TOKENS = {
    model: pricing.model_dump()
    for p in PROVIDER_SETTINGS.values() if p.type == "openai"
    for model, pricing in p.pricing_usd_per_million_tokens.items()
}
