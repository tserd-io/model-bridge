from datetime import datetime, timezone
import logging
from typing import Any
import json
from model_bridge.config.models import LoggingSettings

logger = logging.getLogger("model_bridge")
logger.propagate = False
LEVELS = {
    name: getattr(logging, name.upper())
    for name in ("debug", "info", "warning", "error", "critical")
}


# Applies process-wide application logging settings without duplicating handlers.
def configure_logging(settings: LoggingSettings) -> None:
    logger.disabled = not settings.enabled
    logger.setLevel(LEVELS[settings.level])
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


# Emits only operational metadata at the selected severity, with an explicit UTC timestamp.
def log_event(event: str, *, level: str = "info", **fields: Any) -> None:
    if level not in LEVELS:
        raise ValueError(f"Unknown event severity: {level}")
    severity = LEVELS[level]
    if not logger.isEnabledFor(severity):
        return
    timestamp = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    logger.log(
        severity,
        json.dumps(
            {**fields, "event": event, "timestamp": timestamp, "level": level},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
    )
