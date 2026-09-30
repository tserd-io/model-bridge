import json
import os
from pathlib import Path

with Path(__file__).with_name("config.json").open(encoding="utf-8") as config_file:
	_settings = json.load(config_file)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", _settings["ollama_host"])

LLM_PROVIDER = os.getenv("LLM_PROVIDER", _settings["provider"]).lower()
FALLBACK_PROVIDER = os.getenv(
	"LLM_FALLBACK_PROVIDER",
	_settings.get("fallback_provider"),
)
FALLBACK_PROVIDER = FALLBACK_PROVIDER.lower() if FALLBACK_PROVIDER else None

DEFAULT_LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", _settings["llm_timeout_seconds"]))

REQUEST_DEADLINE_SECONDS = float(os.getenv("REQUEST_DEADLINE_SECONDS", _settings["request_deadline_seconds"]))

MAX_LLM_ATTEMPTS = int(os.getenv("MAX_LLM_ATTEMPTS", _settings["max_llm_attempts"]))

RETRY_BASE_DELAY_SECONDS = float(
	os.getenv("RETRY_BASE_DELAY_SECONDS", _settings["retry_base_delay_seconds"])
)
IDEMPOTENCY_DB_PATH = os.getenv(
	"IDEMPOTENCY_DB_PATH",
	str(Path(__file__).resolve().parents[1] / "gateway_requests.sqlite3"),
)
MODEL_PREFERENCES = {
	preference: {
		provider: os.getenv(f"{provider.upper()}_MODEL_{preference.upper()}", model)
		for provider, model in provider_models.items()
	}
	for preference, provider_models in _settings["model_preferences"].items()
}
MODEL_PRICING_USD_PER_MILLION_TOKENS = json.loads(
	os.getenv(
		"MODEL_PRICING_USD_PER_MILLION_TOKENS",
		json.dumps(_settings.get("model_pricing_usd_per_million_tokens", {})),
	)
)
