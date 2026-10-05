"""Validated settings models consumed by the loader and application composition.

Settings.from_config_dict accepts the current config.json layout. Provider
credentials are deliberately excluded. Environment overrides belong in the
loader and must be applied before validation.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SettingsModel(BaseModel):
    """Common validation rules for every configuration category.

    Unknown fields are rejected to expose misspelled settings at startup.
    Non-finite numbers are rejected so timeouts and budgets remain meaningful.
    Frozen models prevent field reassignment after validation, but do not make
    nested dictionaries immutable: callers must treat those mappings as read-only.
    These models describe configuration; they do not enforce runtime behavior.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RateLimitSettings(SettingsModel):
    """Admission frequency: how many requests may enter within a time window.

    Reused for platform and tenant policies. A limiter instance must maintain
    the actual request history; this object contains only its parameters.
    Rate limits are distinct from concurrency limits: a slow request can stay
    active after its admission has aged out of the rate-limit window.
    The enforcement scope depends on whether that limiter is local or shared.
    """

    requests: int = Field(default=3, ge=1)
    window_seconds: float = Field(default=10, gt=0)


class LoggingSettings(SettingsModel):
    """Operator-controlled application logging switches.

    Enabled controls event emission, while level selects the minimum severity.
    The application must configure its logger from these values during startup.
    Neither enabling logs nor choosing debug level authorizes logging credentials,
    complete settings objects, or private request content.
    """

    enabled: bool = True
    level: Literal["debug", "info", "warning", "error", "critical"] = "info"


class MetricsSettings(SettingsModel):
    """Controls application instrumentation and tenant-level metric detail.

    Enabled must be wired to metric recording and endpoint registration.
    Tenant labels default off because each distinct tenant creates additional
    time series and consumes memory. When disabled, instrumentation should omit
    that dimension or use one fixed label value. This model does not configure
    a separate HTTP listener or restrict network access to the metrics endpoint.
    """

    enabled: bool = True
    include_tenant_label: bool = False
    tenant_label_allowlist: tuple[str, ...] = Field(default=(), max_length=100,)


class PlatformSettings(SettingsModel):
    """Shared execution capacity and ceilings that tenant policies cannot exceed.

    Concurrency bounds active jobs in one application process. Multiple workers
    or replicas multiply that capacity unless enforcement is coordinated.
    Input bytes and output tokens bound different quantities; consumers must
    define which input representation is counted and enforce both limits.
    Request deadline is the total execution budget, including attempts and retry
    delays. Shutdown grace is time allowed for draining work during termination.
    Logging, metrics, and admission-rate policy are shared operator settings.
    Docker/Kubernetes CPU, memory, and replica settings remain deployment concerns.
    """

    # This limit applies to one process, not all workers or replicas.
    max_concurrent_jobs_per_instance: int = Field(default=5, ge=1)
    max_input_bytes: int = Field(default=1048576, ge=1)
    max_message_characters: int = Field(default=32768, ge=1)
    max_output_tokens: int = Field(default=8192, ge=1)
    request_deadline_seconds: float = Field(default=45, gt=0)
    shutdown_grace_seconds: float = Field(default=50, gt=0)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)


class TenantLimits(SettingsModel):
    """Complete effective execution policy for one authenticated tenant.

    Defaults supply these allowances; tenant overrides may replace selected
    values. The root Settings model checks concurrency, size, token, and deadline
    allowances against platform ceilings. Services still need to acquire slots,
    reject oversized input, and apply deadlines to enforce this policy.
    This object holds neither tenant identity nor mutable limiter state. Obtain
    identity from trusted authentication before resolving the tenant's limits.
    No field represents a hard per-tenant memory limit in a shared process.
    """

    max_concurrent_jobs: int = Field(default=2, ge=1)
    max_input_bytes: int = Field(default=1048576, ge=1)
    max_output_tokens: int = Field(default=2048, ge=1)
    request_deadline_seconds: float = Field(default=45, gt=0)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)


class TenantLimitsOverride(SettingsModel):
    """Sparse operator-defined changes to the default tenant policy.

    Omitted or None fields inherit their values from TenantLimits. Resolution
    produces a complete, validated TenantLimits object before execution.
    Rate-limit overrides replace that nested object as a whole; they are not a
    recursive partial merge. Unknown tenant IDs currently receive the defaults,
    so this mapping is a policy lookup, not a registry of authorized tenants.
    """

    max_concurrent_jobs: int | None = Field(default=None, ge=1)
    max_input_bytes: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    request_deadline_seconds: float | None = Field(default=None, gt=0)
    rate_limit: RateLimitSettings | None = None


class ModelPricing(SettingsModel):
    """Nonnegative USD rates per million input and output tokens.

    Provider-reported token usage and these rates support cost estimates.
    They do not enforce spending limits or guarantee agreement with a bill.
    Operators must maintain rates as pricing changes; zero means an explicitly
    configured zero rate, while an absent model entry means pricing is unknown.
    """

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class ProviderSettings(SettingsModel):
    """Connection, model selection, and resilience policy for one backend.

    Type selects an adapter; models maps gateway preferences to backend model
    names. Real providers require fast and balanced mappings, while fake supports
    isolated development without network calls. Ollama requires an endpoint;
    the current OpenAI adapter uses its SDK default instead.
    Attempt timeout bounds one call. Max attempts includes the initial call;
    retry delays must also fit within the effective request deadline. Circuit
    breaker settings describe failure thresholds and cooldown, not breaker state.
    Credentials are intentionally absent and belong in a secret source. Endpoints
    must remain operator-controlled; presence validation is not URL validation,
    endpoint allowlisting, or protection against arbitrary outbound access.
    """

    type: Literal["ollama", "openai", "fake"]
    endpoint: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    attempt_timeout_seconds: float = Field(default=30, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    retry_base_delay_seconds: float = Field(default=0.25, ge=0)
    circuit_breaker_failure_threshold: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_seconds: float = Field(default=30, gt=0)
    pricing_usd_per_million_tokens: dict[str, ModelPricing] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_models(self) -> "ProviderSettings":
        if self.type != "fake" and not all (
            self.models.get(key, "").strip() for key in ("fast", "balanced")):
                raise ValueError("Provider needs nonempty fast and balanced model names")
        if self.type == "ollama" and not self.endpoint:
            raise ValueError("Ollama needs an endpoint")
        return self


class StorageSettings(SettingsModel):
    """SQLite request-storage location and timing parameters.

    Resolve relative paths against an explicit base directory so launches from
    different working directories select the same database. Busy timeout bounds
    waiting for a database lock. Lease margin adds time beyond the request budget
    before an unfinished request is considered stale; it is not result retention.
    This model neither opens storage nor configures cleanup or tenant isolation.
    Persistent volumes, filesystem permissions, and multi-replica storage design
    must be handled by the deployment and storage implementation.
    """

    path: Path = Path("./data/gateway_requests.sqlite3")
    busy_timeout_seconds: float = Field(default=5, gt=0)
    lease_margin_seconds: float = Field(default=5, gt=0)

    def resolved_path(self, base_directory: Path) -> Path:
        """Resolve relative paths against an explicit loader-provided directory."""
        return (base_directory / self.path).resolve()


class Settings(SettingsModel):
    """Root configuration composed and validated before runtime objects are built.

    Named provider entries allow primary and fallback selection independently
    of adapter type. Validation checks those references and every resolved tenant
    policy against platform ceilings. Tenant rate policy is independent of the
    platform rate policy; runtime admission must satisfy both.
    from_config_dict adapts the existing flat JSON layout or accepts the grouped
    layout. The loader owns file reading, environment precedence, and secrets.
    Pass relevant nested models into services rather than exposing this entire
    object to requests. Construct providers, limiters, and storage separately:
    validated settings alone do not authenticate tenants or enforce limits.
    """

    platform: PlatformSettings = Field(default_factory=PlatformSettings)
    tenant_defaults: TenantLimits = Field(default_factory=TenantLimits)
    tenant_overrides: dict[str, TenantLimitsOverride] = Field(default_factory=dict)
    providers: dict[str, ProviderSettings]
    primary_provider: str
    fallback_provider: str | None = None
    storage: StorageSettings = Field(default_factory=StorageSettings)

    def effective_tenant_limits(self, tenant_id: str) -> TenantLimits:
        """Resolve policy only; the caller must authenticate tenant identity."""
        override = self.tenant_overrides.get(tenant_id)
        values = self.tenant_defaults.model_dump()
        if override is not None:
            values.update(override.model_dump(exclude_none=True))
        return TenantLimits.model_validate(values)

    @model_validator(mode="after")
    def validate_references_and_limits(self) -> "Settings":
        for name in (self.primary_provider, self.fallback_provider):
            if name is not None and name not in self.providers:
                raise ValueError(f"Unknown provider: {name}")
        for tenant_id in (None, *self.tenant_overrides):
            limits = self.tenant_defaults if tenant_id is None else self.effective_tenant_limits(tenant_id)
            ceilings = {
                "max_concurrent_jobs": self.platform.max_concurrent_jobs_per_instance,
                "max_input_bytes": self.platform.max_input_bytes,
                "max_output_tokens": self.platform.max_output_tokens,
                "request_deadline_seconds": self.platform.request_deadline_seconds,
            }
            for field, ceiling in ceilings.items():
                if getattr(limits, field) > ceiling:
                    raise ValueError(f"Tenant {tenant_id or 'defaults'}: {field} exceeds platform ceiling")
        return self

    @classmethod
    def from_config_dict(cls, config: dict[str, Any]) -> "Settings":
        """Normalize current flat provider keys, then validate the grouped model.

        Also accepts the future grouped layout directly. Conflicting duplicate
        deadlines and unknown top-level keys are rejected rather than ignored.
        """
        if "providers" in config:
            return cls.model_validate(config)
        data = dict(config)
        primary = data.pop("provider")
        fallback = data.pop("fallback_provider", None)
        host = data.pop("ollama_host")
        preferences = data.pop("model_preferences")
        pricing = data.pop("model_pricing_usd_per_million_tokens", {})
        deadline = data.pop("request_deadline_seconds", None)
        platform = dict(data.pop("platform", {}))
        if deadline is not None:
            if "request_deadline_seconds" in platform and platform["request_deadline_seconds"] != deadline:
                raise ValueError("Conflicting top-level and platform request deadlines")
            platform.setdefault("request_deadline_seconds", deadline)
        platform.setdefault("rate_limit", {
            "requests": data.pop("rate_limit_requests", 3),
            "window_seconds": data.pop("rate_limit_window_seconds", 10),
        })
        provider_defaults = {
            "attempt_timeout_seconds": data.pop("llm_timeout_seconds", 30),
            "max_attempts": data.pop("max_llm_attempts", 3),
            "retry_base_delay_seconds": data.pop("retry_base_delay_seconds", 0.25),
            "circuit_breaker_failure_threshold": data.pop("circuit_breaker_failure_threshold", 5),
            "circuit_breaker_cooldown_seconds": data.pop("circuit_breaker_cooldown_seconds", 30),
        }
        names = {name for mapping in preferences.values() for name in mapping}
        names.update(name for name in (primary, fallback) if name is not None)
        providers = {
            name: {
                **provider_defaults,
                "type": name,
                "endpoint": host if name == "ollama" else None,
                "models": {key: mapping[name] for key, mapping in preferences.items() if name in mapping},
                "pricing_usd_per_million_tokens": pricing if name == "openai" else {},
            }
            for name in names
        }
        return cls.model_validate({
            **data,
            "platform": platform,
            "providers": providers,
            "primary_provider": primary,
            "fallback_provider": fallback,
        })
