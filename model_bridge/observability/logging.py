import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("model_bridge")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(stream_handler)


# Emits a structured event with an explicit UTC timestamp.
def log_event(event: str, **fields: Any) -> None:
    timestamp = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    logger.info(
        json.dumps(
            {
                **fields,
                "event": event,
                "timestamp": timestamp,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )
