"""Environment-backed configuration for the Mini App API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("TELEGRAMAIL_ADMIN_TELEGRAM_USER_ID must be an integer") from exc


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """All HTTP settings have explicit, safe defaults for local development.

    ``telegram_bot_token`` intentionally has no fallback: without it Telegram's
    initData cannot be verified and setup authentication is unavailable.
    """

    session_secret: str
    telegram_bot_token: str | None = None
    admin_telegram_user_id: int | None = None
    setup_code: str | None = None
    webhook_secret: str | None = None
    init_data_max_age_seconds: int = 300
    session_ttl_seconds: int = 86_400
    secure_cookies: bool = True
    web_dist: Path | None = None
    web_base_url: str | None = None
    telegram_bot_api_proxy: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        dist = os.getenv("TELEGRAMAIL_WEB_DIST")
        raw_age = os.getenv("TELEGRAMAIL_INIT_DATA_MAX_AGE_SECONDS", "300")
        raw_ttl = os.getenv("TELEGRAMAIL_SESSION_TTL_SECONDS", "86400")
        settings = cls(
            session_secret=os.getenv("SESSION_SECRET") or os.getenv("TELEGRAMAIL_SESSION_SECRET", ""),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            admin_telegram_user_id=_optional_int(
                os.getenv("TELEGRAMAIL_ADMIN_TELEGRAM_USER_ID")
            ),
            setup_code=os.getenv("SETUP_CODE") or os.getenv("TELEGRAMAIL_SETUP_CODE") or None,
            webhook_secret=(
                os.getenv("TELEGRAM_WEBHOOK_SECRET")
                or os.getenv("TELEGRAMAIL_WEBHOOK_SECRET")
                or None
            ),
            init_data_max_age_seconds=int(raw_age),
            session_ttl_seconds=int(raw_ttl),
            secure_cookies=_as_bool(os.getenv("TELEGRAMAIL_SECURE_COOKIES"), True),
            web_dist=Path(dist) if dist else Path("web/dist"),
            web_base_url=os.getenv("WEB_BASE_URL") or None,
            telegram_bot_api_proxy=os.getenv("TELEGRAMAIL_BOT_API_PROXY") or None,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        if self.init_data_max_age_seconds <= 0:
            raise ValueError("TELEGRAMAIL_INIT_DATA_MAX_AGE_SECONDS must be positive")
        if self.session_ttl_seconds <= 0:
            raise ValueError("TELEGRAMAIL_SESSION_TTL_SECONDS must be positive")
