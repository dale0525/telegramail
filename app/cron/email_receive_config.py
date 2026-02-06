import os

from app.utils import Logger

logger = Logger().get_logger(__name__)

DEFAULT_POLLING_INTERVAL_SECONDS = 300
MIN_POLLING_INTERVAL_SECONDS = 10
DEFAULT_MAIL_RECEIVE_MODE = "hybrid"
VALID_MAIL_RECEIVE_MODES = {"polling", "idle", "hybrid"}
DEFAULT_IMAP_IDLE_TIMEOUT_SECONDS = 1740
DEFAULT_IMAP_IDLE_FALLBACK_POLL_SECONDS = 30
DEFAULT_IMAP_IDLE_RECONNECT_BACKOFF_SECONDS = 5


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


def get_mail_receive_mode() -> str:
    raw = (os.getenv("MAIL_RECEIVE_MODE") or "").strip().lower()
    if not raw:
        return DEFAULT_MAIL_RECEIVE_MODE

    if raw not in VALID_MAIL_RECEIVE_MODES:
        logger.warning(
            f"Invalid MAIL_RECEIVE_MODE '{raw}', using default '{DEFAULT_MAIL_RECEIVE_MODE}'"
        )
        return DEFAULT_MAIL_RECEIVE_MODE

    return raw


def _parse_int_env_with_min(name: str, default: int, min_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"Invalid {name} '{raw}', using default {default}")
        return default

    if value < min_value:
        logger.warning(f"{name} {value} is too small, clamping to {min_value}")
        return min_value
    return value


def get_imap_idle_timeout_seconds() -> int:
    return _parse_int_env_with_min(
        "IMAP_IDLE_TIMEOUT_SECONDS", DEFAULT_IMAP_IDLE_TIMEOUT_SECONDS, min_value=10
    )


def get_imap_idle_fallback_poll_seconds() -> int:
    return _parse_int_env_with_min(
        "IMAP_IDLE_FALLBACK_POLL_SECONDS",
        DEFAULT_IMAP_IDLE_FALLBACK_POLL_SECONDS,
        min_value=1,
    )


def get_imap_idle_reconnect_backoff_seconds() -> int:
    return _parse_int_env_with_min(
        "IMAP_IDLE_RECONNECT_BACKOFF_SECONDS",
        DEFAULT_IMAP_IDLE_RECONNECT_BACKOFF_SECONDS,
        min_value=1,
    )
