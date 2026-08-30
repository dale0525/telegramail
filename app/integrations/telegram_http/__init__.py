"""Async HTTP integration for the Telegram Bot API."""

from .client import (
    MAX_DOCUMENT_BYTES,
    MAX_MESSAGE_LENGTH,
    MiniAppOnlyProjection,
    TelegramApiError,
    TelegramBotApiClient,
    TelegramUpdateDispatcher,
    is_missing_forum_topic_error,
    split_html,
    split_text,
    verify_webhook_secret,
)

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "MAX_MESSAGE_LENGTH",
    "MiniAppOnlyProjection",
    "TelegramApiError",
    "TelegramBotApiClient",
    "TelegramUpdateDispatcher",
    "is_missing_forum_topic_error",
    "split_html",
    "split_text",
    "verify_webhook_secret",
]
