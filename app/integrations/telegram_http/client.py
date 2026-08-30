"""Small, TDLib-free async client for the Telegram Bot API.

The module deliberately keeps the Bot API boundary narrow.  It never logs the
bot token or webhook secret, and accepts an ``httpx`` transport so callers can
exercise the complete HTTP contract without reaching Telegram.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable, Mapping, Sequence

import httpx


MAX_MESSAGE_LENGTH = 4096
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_HTML_TOKEN_RE = re.compile(r"(<[^>]*>|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);)")
_HTML_TAG_RE = re.compile(r"^<\s*(/)?\s*([a-zA-Z][a-zA-Z0-9:-]*)(?:\s[^>]*)?>$")
_VOID_HTML_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}


class TelegramApiError(RuntimeError):
    """A normalized Telegram Bot API or transport error.

    ``ambiguous`` is true when a timeout or connection failure may have reached
    Telegram.  Such operations are intentionally not retried automatically.
    """

    def __init__(
        self,
        method: str,
        description: str,
        *,
        status_code: int | None = None,
        error_code: int | None = None,
        retry_after: int | None = None,
        ambiguous: bool = False,
    ) -> None:
        self.method = method
        self.description = description
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after
        self.ambiguous = ambiguous
        code = error_code if error_code is not None else status_code
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"Telegram {method} failed: {description}{suffix}")


def is_missing_forum_topic_error(error: BaseException) -> bool:
    """Return whether Telegram definitively rejected a stale Topic mapping."""

    description = str(getattr(error, "description", error)).casefold()
    return "topic_id_invalid" in description or "message thread not found" in description


@dataclass(frozen=True)
class MiniAppOnlyProjection:
    """Result used when a file exceeds the Bot API's 50 MiB document limit."""

    size_bytes: int
    filename: str | None = None
    delivery: str = "mini_app_only"

    @property
    def mini_app_only(self) -> bool:
        return True


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
UpdateHandler = Callable[[dict[str, Any]], Awaitable[Any]]
MessageHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


def verify_webhook_secret(
    headers: Mapping[str, str], expected_secret: str | None
) -> bool:
    """Constant-time verification of Telegram's webhook secret header."""

    if not expected_secret:
        return False
    received = next(
        (
            value
            for name, value in headers.items()
            if name.lower() == "x-telegram-bot-api-secret-token"
        ),
        None,
    )
    return isinstance(received, str) and hmac.compare_digest(received, expected_secret)


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text on Unicode code point boundaries without losing any content."""

    if limit < 1:
        raise ValueError("limit must be positive")
    if not text:
        return [""]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _html_token_length(token: str) -> int:
    if token.startswith("<"):
        return 0
    if token.startswith("&") and token.endswith(";"):
        return 1
    return len(token)


def _tag_state(token: str) -> tuple[str, str, str] | None:
    match = _HTML_TAG_RE.match(token)
    if not match:
        return None
    closing, name = match.groups()
    normalized = name.lower()
    if closing:
        return ("close", normalized, token)
    if token.rstrip().endswith("/>") or normalized in _VOID_HTML_TAGS:
        return ("void", normalized, token)
    return ("open", normalized, token)


def split_html(html: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split Telegram HTML while keeping every output fragment balanced.

    Telegram limits the rendered text length.  Tags contribute zero characters
    and entities contribute one; active tags are closed and re-opened at a
    fragment boundary, so no entity, tag, or Unicode character is cut in half.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    if not html:
        return [""]

    tokens: list[str] = []
    position = 0
    for match in _HTML_TOKEN_RE.finditer(html):
        if match.start() > position:
            tokens.extend(html[position : match.start()])
        tokens.append(match.group(0))
        position = match.end()
    if position < len(html):
        tokens.extend(html[position:])

    fragments: list[str] = []
    parts: list[str] = []
    active: list[tuple[str, str]] = []
    rendered_length = 0

    def close_active() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(active))

    def start_fragment() -> None:
        nonlocal parts, rendered_length
        parts = [opening for _, opening in active]
        rendered_length = 0

    def flush() -> None:
        nonlocal parts
        if parts or active:
            fragments.append("".join(parts) + close_active())
        start_fragment()

    for token in tokens:
        token_length = _html_token_length(token)
        if token_length and rendered_length + token_length > limit:
            flush()
        parts.append(token)
        rendered_length += token_length
        state = _tag_state(token)
        if state is None:
            continue
        kind, name, opening = state
        if kind == "open":
            active.append((name, opening))
        elif kind == "close":
            for index in range(len(active) - 1, -1, -1):
                if active[index][0] == name:
                    del active[index:]
                    break

    if parts or active:
        fragments.append("".join(parts) + close_active())
    return fragments or [""]


class TelegramUpdateDispatcher:
    """Minimal, production-safe dispatcher for Bot API webhook updates.

    The dispatcher intentionally handles only Bot API dictionaries.  It does
    not adapt TDLib update objects or import aiotdlib, so the webhook process has
    no user-session dependency.
    """

    def __init__(
        self,
        client: "TelegramBotApiClient",
        *,
        mini_app_url: str | None = None,
        message_handler: MessageHandler | None = None,
        callback_handler: Callable[[dict[str, Any]], Awaitable[Any] | Any] | None = None,
    ) -> None:
        self.client = client
        self.mini_app_url = mini_app_url
        self.message_handler = message_handler
        self.callback_handler = callback_handler

    async def handle(self, update: dict[str, Any]) -> bool:
        message = update.get("message")
        if isinstance(message, Mapping):
            return await self._handle_message(message)
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            return await self._handle_callback(callback)
        # Telegram may send update kinds this bot has not opted into.  Treating
        # them as successfully ignored prevents a poison update from retrying.
        return True

    async def _handle_message(self, message: Mapping[str, Any]) -> bool:
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or chat.get("id") is None:
            raise TelegramApiError("dispatchUpdate", "message has no chat id")
        text = message.get("text")
        if not isinstance(text, str):
            return True
        command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        chat_id = chat["id"]
        if command == "/start":
            await self._set_menu(chat_id)
            if not await self._dispatch_message_handler(message):
                await self.client.send_message(chat_id, "Telegramail 已连接。点击菜单按钮打开 Mini App。")
        elif not await self._dispatch_message_handler(message):
            return True
        return True

    async def _dispatch_message_handler(self, message: Mapping[str, Any]) -> bool:
        if self.message_handler is None:
            return False
        result = self.message_handler(dict(message))
        if hasattr(result, "__await__"):
            result = await result
        return result is not False

    async def _set_menu(self, chat_id: int | str) -> None:
        menu_button: dict[str, Any] = {"type": "commands"}
        if self.mini_app_url:
            menu_button = {
                "type": "web_app",
                "text": "TelegramMail",
                "web_app": {"url": self.mini_app_url},
            }
        await self.client.set_chat_menu_button(menu_button, chat_id=chat_id)

    async def _handle_callback(self, callback: Mapping[str, Any]) -> bool:
        callback_id = callback.get("id")
        if not isinstance(callback_id, str) or not callback_id:
            raise TelegramApiError("dispatchUpdate", "callback query has no id")
        answer: Mapping[str, Any] = {}
        if self.callback_handler is not None:
            result = self.callback_handler(dict(callback))
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, Mapping):
                answer = result
        allowed = {"text", "show_alert", "url", "cache_time"}
        await self.client.answer_callback_query(
            callback_id,
            **{key: value for key, value in answer.items() if key in allowed},
        )
        return True


class TelegramBotApiClient:
    """A retry-aware async client for the Telegram HTTP Bot API."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        timeout: httpx.TimeoutTypes = 20.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        chat_message_interval: float = 1.0,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        mini_app_url: str | None = None,
        message_handler: MessageHandler | None = None,
        callback_handler: Callable[[dict[str, Any]], Awaitable[Any] | Any] | None = None,
        update_handler: UpdateHandler | None = None,
    ) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/",
            transport=transport,
            proxy=proxy,
            timeout=timeout,
        )
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._chat_message_interval = chat_message_interval
        self._sleep = sleep
        self._clock = clock
        self._chat_locks: dict[int | str, asyncio.Lock] = {}
        self._last_chat_message_at: dict[int | str, float] = {}
        self._update_handler = update_handler
        self._dispatcher = TelegramUpdateDispatcher(
            self,
            mini_app_url=mini_app_url,
            message_handler=message_handler,
            callback_handler=callback_handler,
        )

    async def __aenter__(self) -> "TelegramBotApiClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def handle_update(self, update: dict[str, Any]) -> Any:
        """Dispatch one webhook update; errors propagate for durable retry."""

        if not isinstance(update, dict):
            raise TelegramApiError("dispatchUpdate", "update must be an object")
        if self._update_handler is not None:
            return await self._update_handler(update)
        return await self._dispatcher.handle(update)

    async def _throttle_chat(self, chat_id: int | str) -> None:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            now = self._clock()
            next_allowed = self._last_chat_message_at.get(chat_id, float("-inf")) + self._chat_message_interval
            delay = max(0.0, next_allowed - now)
            if delay:
                await self._sleep(delay)
            self._last_chat_message_at[chat_id] = max(now, next_allowed)

    @staticmethod
    def _error_from_response(method: str, response: httpx.Response) -> TelegramApiError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        parameters = body.get("parameters") if isinstance(body, dict) else None
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if not isinstance(retry_after, int):
            retry_after = None
        error_code = body.get("error_code") if isinstance(body, dict) else None
        if not isinstance(error_code, int):
            error_code = None
        description = body.get("description") if isinstance(body, dict) else None
        if not isinstance(description, str):
            description = response.reason_phrase or "Telegram returned an invalid response"
        return TelegramApiError(
            method,
            description,
            status_code=response.status_code,
            error_code=error_code,
            retry_after=retry_after,
        )

    async def _call(
        self,
        method: str,
        payload: Mapping[str, Any] | None = None,
        *,
        files: Mapping[str, Any] | None = None,
        ambiguous_on_transport_error: bool = True,
    ) -> Any:
        for attempt in range(self._max_retries + 1):
            try:
                if files is None:
                    response = await self._client.post(method, json=payload or {})
                else:
                    response = await self._client.post(method, data=payload or {}, files=files)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise TelegramApiError(
                    method,
                    "request timed out" if isinstance(exc, httpx.TimeoutException) else "transport error",
                    ambiguous=ambiguous_on_transport_error,
                ) from exc

            error = self._error_from_response(method, response)
            if response.is_success:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise TelegramApiError(method, "invalid JSON response", status_code=response.status_code) from exc
                if isinstance(body, dict) and body.get("ok") is True:
                    return body.get("result")
                # Telegram may return HTTP 200 with {ok: false}.
                error = self._error_from_response(method, response)

            retry_delay: float | None = None
            is_rate_limited = error.error_code == 429 or response.status_code == 429
            is_server_error = response.status_code >= 500
            if is_rate_limited and error.retry_after is not None:
                retry_delay = float(error.retry_after)
            elif is_server_error:
                retry_delay = self._retry_backoff * (2**attempt)
            if retry_delay is not None and attempt < self._max_retries:
                await self._sleep(retry_delay)
                continue
            raise error
        raise AssertionError("retry loop exhausted")

    async def get_me(self, *, require_topics_enabled: bool = False) -> Mapping[str, Any]:
        result = await self._call("getMe", ambiguous_on_transport_error=False)
        if not isinstance(result, Mapping):
            raise TelegramApiError("getMe", "invalid result shape")
        if require_topics_enabled and result.get("has_topics_enabled") is not True:
            raise TelegramApiError("getMe", "bot does not have topics enabled")
        return result

    async def ensure_topics_enabled(self) -> Mapping[str, Any]:
        """Gate forum-topic usage on the explicit Bot API capability."""

        return await self.get_me(require_topics_enabled=True)

    async def set_webhook(
        self,
        url: str,
        *,
        secret_token: str | None = None,
        allowed_updates: Sequence[str] | None = None,
        drop_pending_updates: bool = False,
    ) -> Any:
        payload: dict[str, Any] = {"url": url, "drop_pending_updates": drop_pending_updates}
        if secret_token is not None:
            payload["secret_token"] = secret_token
        if allowed_updates is not None:
            payload["allowed_updates"] = list(allowed_updates)
        return await self._call("setWebhook", payload)

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> Any:
        return await self._call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    async def set_my_commands(self, commands: Sequence[Mapping[str, str]]) -> Any:
        return await self._call(
            "setMyCommands",
            {"commands": [dict(command) for command in commands]},
        )

    async def set_chat_menu_button(
        self, menu_button: Mapping[str, Any], *, chat_id: int | str | None = None
    ) -> Any:
        payload: dict[str, Any] = {"menu_button": dict(menu_button)}
        if chat_id is not None:
            payload["chat_id"] = chat_id
        return await self._call("setChatMenuButton", payload)

    async def create_forum_topic(
        self,
        chat_id: int | str,
        name: str,
        *,
        icon_color: int | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> Mapping[str, Any]:
        await self.ensure_topics_enabled()
        payload: dict[str, Any] = {"chat_id": chat_id, "name": name}
        if icon_color is not None:
            payload["icon_color"] = icon_color
        if icon_custom_emoji_id is not None:
            payload["icon_custom_emoji_id"] = icon_custom_emoji_id
        result = await self._call("createForumTopic", payload)
        if not isinstance(result, Mapping):
            raise TelegramApiError("createForumTopic", "invalid result shape")
        return result

    async def edit_forum_topic(
        self,
        chat_id: int | str,
        message_thread_id: int,
        *,
        name: str | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> Any:
        await self.ensure_topics_enabled()
        payload: dict[str, Any] = {"chat_id": chat_id, "message_thread_id": message_thread_id}
        if name is not None:
            payload["name"] = name
        if icon_custom_emoji_id is not None:
            payload["icon_custom_emoji_id"] = icon_custom_emoji_id
        return await self._call("editForumTopic", payload)

    async def delete_forum_topic(self, chat_id: int | str, message_thread_id: int) -> Any:
        await self.ensure_topics_enabled()
        return await self._call(
            "deleteForumTopic", {"chat_id": chat_id, "message_thread_id": message_thread_id}
        )

    async def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": int(message_id), "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return await self._call("editMessageText", payload)

    async def edit_message_caption(
        self,
        chat_id: int | str,
        message_id: int,
        caption: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Any:
        """Edit a document/photo caption in place.

        HTML mail projections use a document caption as their single primary
        message. Keeping this Bot API call separate from ``editMessageText``
        avoids Telegram's ``message is not a text message`` error when an LLM
        summary is refreshed later.
        """

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "caption": caption,
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return await self._call("editMessageCaption", payload)

    async def edit_message_reply_markup(
        self,
        chat_id: int | str,
        message_id: int,
        *,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Any:
        return await self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": int(message_id), "reply_markup": dict(reply_markup or {})},
        )

    async def delete_message(self, chat_id: int | str, message_id: int) -> Any:
        return await self._call(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
        )

    async def pin_chat_message(
        self,
        chat_id: int | str,
        message_id: int,
        *,
        disable_notification: bool = True,
    ) -> Any:
        return await self._call(
            "pinChatMessage",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "disable_notification": disable_notification,
            },
        )

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        **extra: Any,
    ) -> list[Any]:
        chunks = split_html(text) if parse_mode and parse_mode.upper() == "HTML" else split_text(text)
        results: list[Any] = []
        for chunk in chunks:
            await self._throttle_chat(chat_id)
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk, **extra}
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            results.append(await self._call("sendMessage", payload))
        return results

    async def send_document(
        self,
        chat_id: int | str,
        document: bytes | bytearray | memoryview | BinaryIO | str | Path,
        *,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
        message_thread_id: int | None = None,
        caption: str | None = None,
        **extra: Any,
    ) -> Any | MiniAppOnlyProjection:
        file_object, inferred_filename, size, should_close = self._document_file(document, filename)
        if size > MAX_DOCUMENT_BYTES:
            if should_close:
                file_object.close()
            return MiniAppOnlyProjection(size_bytes=size, filename=inferred_filename)
        try:
            await self._throttle_chat(chat_id)
            payload: dict[str, Any] = {"chat_id": chat_id, **extra}
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            if caption is not None:
                payload["caption"] = caption
            # httpx encodes ``data=`` fields in multipart requests as scalar
            # values.  Telegram expects structured fields such as
            # ``reply_markup`` as JSON strings for multipart methods (unlike
            # the nested JSON body used by ``send_message``/edit calls).
            multipart_payload = {
                key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if isinstance(value, (Mapping, list, tuple))
                else value
                for key, value in payload.items()
            }
            return await self._call(
                "sendDocument",
                multipart_payload,
                files={"document": (inferred_filename or "document", file_object, content_type)},
            )
        finally:
            if should_close:
                file_object.close()

    @staticmethod
    def _document_file(
        document: bytes | bytearray | memoryview | BinaryIO | str | Path,
        filename: str | None,
    ) -> tuple[BinaryIO, str | None, int, bool]:
        if isinstance(document, (str, Path)):
            path = Path(document)
            return path.open("rb"), filename or path.name, path.stat().st_size, True
        if isinstance(document, (bytes, bytearray, memoryview)):
            data = bytes(document)
            return io.BytesIO(data), filename, len(data), True
        if not (hasattr(document, "read") and hasattr(document, "seek") and hasattr(document, "tell")):
            raise TypeError("document must be bytes, a path, or a seekable binary file")
        current = document.tell()
        document.seek(0, os.SEEK_END)
        size = document.tell()
        document.seek(current)
        return document, filename, size, False

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
        url: str | None = None,
        cache_time: int | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text is not None:
            payload["text"] = text
        if url is not None:
            payload["url"] = url
        if cache_time is not None:
            payload["cache_time"] = cache_time
        return await self._call("answerCallbackQuery", payload)
