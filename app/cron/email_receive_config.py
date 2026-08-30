import os

from app.utils import Logger

logger = Logger().get_logger(__name__)

DEFAULT_POLLING_INTERVAL_SECONDS = 300
MIN_POLLING_INTERVAL_SECONDS = 10


def get_polling_interval_seconds() -> int:
    raw = (os.getenv("POLLING_INTERVAL") or "").strip()
    if not raw:
        return DEFAULT_POLLING_INTERVAL_SECONDS

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            f"Invalid POLLING_INTERVAL '{raw}', using default {DEFAULT_POLLING_INTERVAL_SECONDS}s"
        )
        return DEFAULT_POLLING_INTERVAL_SECONDS

    if value < MIN_POLLING_INTERVAL_SECONDS:
        logger.warning(
            f"POLLING_INTERVAL {value}s is too small, clamping to {MIN_POLLING_INTERVAL_SECONDS}s"
        )
        return MIN_POLLING_INTERVAL_SECONDS

    return value
