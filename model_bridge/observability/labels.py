from model_bridge.config.loader import PLATFORM_SETTINGS

LABEL_NAMES = ("provider", "model", "route", "tenant", "error_type", "status_code")
METRICS_SETTINGS = PLATFORM_SETTINGS.metrics
TENANT_LABEL_ALLOWLIST = frozenset(METRICS_SETTINGS.tenant_label_allowlist)


# Bounds tenant labels independently of incoming tenant identities.
def metric_tenant(tenant: str) -> str:
    if not METRICS_SETTINGS.include_tenant_label:
        return "all"

    if tenant in TENANT_LABEL_ALLOWLIST:
        return f"tenant:{tenant}"

    return "other"


# Normalizes metric labels and supplies defaults for missing context.
def _labels(
    provider: str,
    model: str | None,
    route: str,
    tenant: str,
    error_type: str | None,
    status_code: int,
) -> dict[str, str]:
    return {
        "provider": provider or "unknown",
        "model": model or "unknown",
        "route": route or "default",
        "tenant": metric_tenant(tenant),
        "error_type": error_type or "none",
        "status_code": str(status_code),
    }
