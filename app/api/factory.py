"""HTTP API for the Telegram Mini App.

This module keeps persistence behind small duck-typed calls so the v2 store can
be injected without coupling HTTP or authentication semantics to SQLite.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import Settings
from app.core.security import AuthenticationError, Session, issue_session, read_session, verify_telegram_init_data
from app.integrations.telegram_http import verify_webhook_secret


API_PREFIX = "/api/v1"
V2_COMPAT_PREFIX = "/api/v2"
SESSION_COOKIE = "telegramail_session"
CSRF_HEADER = "X-CSRF-Token"
WEBHOOK_PROCESSING_LEASE_SECONDS = 60
logger = logging.getLogger(__name__)
INLINE_ASSET_MIME_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
})


class _DTO(BaseModel):
    model_config = ConfigDict(extra="allow")


class AuthSetupRequest(_DTO):
    init_data: str
    code: str

    @model_validator(mode="before")
    @classmethod
    def accept_telegram_casing(cls, value: Any) -> Any:
        if isinstance(value, dict) and "init_data" not in value and "initData" in value:
            return {**value, "init_data": value["initData"]}
        return value


class AuthSessionRequest(_DTO):
    init_data: str

    @model_validator(mode="before")
    @classmethod
    def accept_telegram_casing(cls, value: Any) -> Any:
        if isinstance(value, dict) and "init_data" not in value and "initData" in value:
            return {**value, "init_data": value["initData"]}
        return value


class AuthStatusResponse(_DTO):
    authenticated: bool
    telegram_user_id: int | None = None
    csrf_token: str | None = None


class AccountInput(_DTO):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str | None = None
    imap_server: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    smtp_server: str = ""
    smtp_port: int = 465
    smtp_ssl: bool = True
    alias: str = ""
    signature: str | None = None


class AccountPatchRequest(_DTO):
    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    password: str | None = None
    imap_server: str | None = None
    imap_port: int | None = None
    imap_ssl: bool | None = None
    smtp_server: str | None = None
    smtp_port: int | None = None
    smtp_ssl: bool | None = None
    alias: str | None = None
    signature: str | None = None


class AccountResponse(_DTO):
    id: int | str | None = None
    email: str
    alias: str = ""
    imap_server: str = ""
    imap_port: int | None = None
    imap_ssl: bool | None = None
    smtp_server: str = ""
    smtp_port: int | None = None
    smtp_ssl: bool | None = None
    signature: str | None = None
    enabled: bool = False
    credential_configured: bool
    connection_status: str = "unknown"
    connection_error: str | None = None
    last_verified_at: int | None = None
    next_verification_at: int | None = None


class AccountDeleteRequest(_DTO):
    model_config = ConfigDict(extra="forbid")

    purge_data: bool = False


class AccountDeleteResponse(_DTO):
    id: int | str
    status: str
    purge_data: bool = False
    error: str | None = None


class LLMSettingsInput(_DTO):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    default_language: str | None = None
    summary_threshold: int | None = Field(default=None, ge=0)
    # Write-only.  Empty values intentionally preserve the encrypted key.
    api_key: str | None = None


class LLMSettingsResponse(_DTO):
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    default_language: str = "en_US"
    summary_threshold: int = 120
    api_key_configured: bool = False
    last_test_status: str = "never"
    last_tested_at: int | None = None
    failed_count: int = 0
    updated_at: int | None = None


class LLMTestResponse(_DTO):
    ok: bool
    status: str | None = None
    last_test_status: str
    last_tested_at: int | None = None
    error: str | None = None


class ContactResponse(_DTO):
    email: str
    name: str | None = None


class InlineAssetResponse(_DTO):
    id: int | str
    content_id: str
    mime_type: str
    size: int


class ThreadResponse(_DTO):
    id: str
    account_id: int | None = None
    subject: str | None = None
    latest_at: str | None = None
    message_count: int = 0
    summary: str | None = None
    summary_status: str | None = None
    priority: str | None = None
    category: str | None = None
    summary_updated_at: int | None = None


class MessageResponse(_DTO):
    id: int | str | None = None
    thread_id: str | None = None
    account_id: int | None = None
    sender: str | None = None
    recipient: str | None = None
    cc: str | None = None
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    email_date: str | None = None
    summary: str | None = None
    summary_status: str | None = None
    priority: str | None = None
    category: str | None = None
    summary_updated_at: int | None = None
    inline_assets: list[InlineAssetResponse] = Field(default_factory=list)


class DraftCreateRequest(_DTO):
    account_id: int
    chat_id: int = 0
    thread_id: int = 0
    draft_type: str = "compose"
    from_identity_email: str = ""
    to_addrs: str | None = None
    cc_addrs: str | None = None
    bcc_addrs: str | None = None
    subject: str | None = None
    body_markdown: str | None = None


class DraftPatchRequest(_DTO):
    to_addrs: str | None = None
    cc_addrs: str | None = None
    bcc_addrs: str | None = None
    subject: str | None = None
    body_markdown: str | None = None
    from_identity_email: str | None = None


class DraftResponse(_DTO):
    id: int | str
    version: int
    account_id: int | None = None
    chat_id: int | None = None
    thread_id: int | None = None
    draft_type: str | None = None
    from_identity_email: str | None = None
    to_addrs: str | None = None
    cc_addrs: str | None = None
    bcc_addrs: str | None = None
    subject: str | None = None
    body_markdown: str | None = None
    status: str | None = None


class AttachmentResponse(_DTO):
    id: int | str
    file_name: str
    mime_type: str | None = None
    size: int | None = None
    status: str
    draft_version: int


class ConnectionCheckResponse(_DTO):
    ok: bool
    error: str | None = None


class AccountVerifyResponse(_DTO):
    imap: ConnectionCheckResponse
    smtp: ConnectionCheckResponse


class OperationResponse(_DTO):
    id: str
    kind: str
    status: Literal["accepted", "queued", "running", "sending", "sent", "deleting", "deleted", "succeeded", "failed", "ambiguous"]
    result: dict[str, Any] | None = None
    error: str | None = None


class BulkDeleteItemRequest(_DTO):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    idempotency_key: str = Field(min_length=1, max_length=255)


class BulkDeleteRequest(_DTO):
    model_config = ConfigDict(extra="forbid")

    items: list[BulkDeleteItemRequest] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def require_unique_thread_ids(self) -> "BulkDeleteRequest":
        normalized = []
        for item in self.items:
            try:
                normalized.append(str(int(item.thread_id)))
            except ValueError:
                normalized.append(item.thread_id.strip())
        if len(set(normalized)) != len(normalized):
            raise ValueError("thread_id must be unique within a bulk delete request")
        return self


class BulkDeleteItemResponse(OperationResponse):
    thread_id: str


class BulkDeleteResponse(_DTO):
    items: list[BulkDeleteItemResponse]


@dataclass
class _RuntimeState:
    used_setup_codes: set[str] = field(default_factory=set)
    draft_versions: dict[str, int] = field(default_factory=dict)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    webhook_updates: dict[int, tuple[str, int]] = field(default_factory=dict)
    sequence: int = 0

    def operation_id(self) -> str:
        self.sequence += 1
        return f"op_{int(time.time() * 1000)}_{self.sequence}"


def _dump(model: BaseModel, *, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)  # pydantic v2
    return model.dict(exclude_unset=exclude_unset)  # pragma: no cover - pydantic v1


def _safe_account(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "email", "alias", "imap_server", "imap_port", "imap_ssl", "smtp_server",
        "smtp_port", "smtp_ssl", "signature", "enabled",
    }
    result = {key: raw.get(key) for key in allowed if key in raw}
    result["email"] = str(result.get("email") or "")
    result["alias"] = str(result.get("alias") or "")
    result["enabled"] = bool(raw.get("enabled", False))
    result["connection_status"] = str(raw.get("connection_status") or ("connected" if result["enabled"] else "unknown"))
    result["connection_error"] = raw.get("connection_error")
    result["last_verified_at"] = raw.get("last_verified_at")
    result["next_verification_at"] = raw.get("next_verification_at")
    result["credential_configured"] = bool(
        raw.get("credential_configured") or raw.get("password") or raw.get("credential") or raw.get("token")
    )
    return result


def _validate_account_transport(values: dict[str, Any]) -> None:
    """Normalize server names and reject incomplete or cleartext transports."""
    for server_key, port_key, tls_key in (
        ("imap_server", "imap_port", "imap_ssl"),
        ("smtp_server", "smtp_port", "smtp_ssl"),
    ):
        server = str(values.get(server_key) or "").strip()
        if (
            not server
            or len(server) > 253
            or any(character.isspace() or character in "/\\@?#" for character in server)
        ):
            raise HTTPException(status_code=422, detail=f"{server_key} must be a non-empty host name")
        port = values.get(port_key)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise HTTPException(status_code=422, detail=f"{port_key} must be between 1 and 65535")
        if values.get(tls_key) is not True and values.get(tls_key) != 1:
            # SMTP submission on 587 negotiates TLS with STARTTLS rather than
            # implicit TLS.  Cleartext IMAP and arbitrary SMTP ports remain
            # rejected so every accepted account still uses encrypted transport.
            if server_key != "smtp_server" or int(port) != 587:
                raise HTTPException(status_code=422, detail=f"{tls_key} must enable TLS/SSL")
        values[server_key] = server


def _call_sync_or_async(target: Any, *args: Any, **kwargs: Any) -> Any:
    value = target(*args, **kwargs)
    return value


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _db_call(db: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        method = getattr(db, name, None)
        if callable(method):
            return _call_sync_or_async(method, *args, **kwargs)
    return None


def _legacy_rows(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Read-only bridge for the existing SQLite manager; omitted for injected stores."""
    connection_factory = getattr(db, "_get_connection", None)
    if not callable(connection_factory):
        return []
    conn = connection_factory()
    try:
        import sqlite3

        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _legacy_one(db: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _legacy_rows(db, sql, params)
    return rows[0] if rows else None


def _v2_rows(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Read adapter for ``app.db.V2Repository`` without importing it directly."""
    database = getattr(db, "db", None)
    connect = getattr(database, "connect", None)
    if not callable(connect):
        return []
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _v2_one(db: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _v2_rows(db, sql, params)
    return rows[0] if rows else None


def _get_default_db() -> Any:
    # Production injects the v2 repository. Tests and lightweight API users may
    # intentionally run without persistence, but must never resurrect the v1 DB.
    return None


def create_app(
    settings: Settings | None = None,
    *,
    db: Any | None = None,
    telegram: Any | None = None,
    imap_transport_factory: Any | None = None,
    smtp_transport_factory: Any | None = None,
) -> FastAPI:
    """Build an independently testable API application.

    ``db`` and ``telegram`` are intentionally injectable.  The default DB bridge
    retains compatibility with the old synchronous SQLite manager.
    """

    active_settings = settings or Settings.from_env()
    active_settings.validate()
    database = db if db is not None else _get_default_db()
    runtime = _RuntimeState()
    app = FastAPI(title="TelegramMail API", version="2.0")
    app.state.settings = active_settings
    app.state.db = database
    app.state.telegram = telegram
    app.state.imap_transport_factory = imap_transport_factory
    app.state.smtp_transport_factory = smtp_transport_factory
    app.state.api_state = runtime
    router = APIRouter()

    def session_from_request(request: Request) -> Session:
        session = read_session(request.cookies.get(SESSION_COOKIE), secret=active_settings.session_secret)
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        if (
            active_settings.admin_telegram_user_id is not None
            and session.user_id != active_settings.admin_telegram_user_id
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this Mini App")
        bound = get_bound_admin()
        if bound is not None and bound != session.user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this Mini App")
        return session

    async def require_session(request: Request) -> Session:
        return session_from_request(request)

    async def require_csrf(
        request: Request,
        session: Session = Depends(require_session),
        csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
    ) -> Session:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not csrf_token or not secrets_compare(csrf_token, session.csrf_token):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
        return session

    def account_rows() -> list[dict[str, Any]]:
        if database is None:
            return []
        result = _db_call(database, ("list_accounts", "get_accounts"))
        if inspect.isawaitable(result):
            raise RuntimeError("Async account stores must expose list_accounts_async")
        return [dict(row) for row in (result or [])]

    def account_by_id(account_id: int) -> dict[str, Any] | None:
        if database is None:
            return None
        getter = getattr(database, "get_account", None) or getattr(database, "get_account_by_id", None)
        if not callable(getter):
            return None
        try:
            result = getter(id=account_id) if getattr(database, "get_account", None) else getter(account_id)
        except TypeError:
            result = getter(account_id)
        if inspect.isawaitable(result):
            raise RuntimeError("Async account stores must expose synchronous API adapters")
        if not result:
            return None
        row = dict(result)
        return None if row.get("deleted_at") is not None else row

    def safe_account(row: dict[str, Any]) -> dict[str, Any]:
        result = _safe_account(row)
        if row.get("id") is not None and getattr(database, "db", None) is not None:
            result["credential_configured"] = bool(
                _v2_one(database, "SELECT 1 AS configured FROM account_secrets WHERE account_id = ?", (int(row["id"]),))
            )
        return result

    def get_bound_admin() -> int | None:
        if database is None:
            return None
        getter = getattr(database, "get_admin_binding", None)
        if not callable(getter):
            return None
        binding = getter()
        if inspect.isawaitable(binding):
            raise RuntimeError("Admin binding store must provide a synchronous API adapter")
        try:
            return int(binding["telegram_user_id"]) if binding else None
        except (KeyError, TypeError, ValueError):
            return None

    def llm_settings_row() -> dict[str, Any] | None:
        """Read the singleton through a duck-typed repository or V2 SQL."""
        if database is None:
            return None
        getter = getattr(database, "get_llm_settings", None)
        if callable(getter):
            try:
                value = getter()
            except TypeError:
                value = getter(singleton=1)
            if inspect.isawaitable(value):
                raise RuntimeError("LLM settings stores must expose a synchronous API adapter")
            return dict(value) if value else None
        return _v2_one(
            database,
            """SELECT enabled, base_url, model, default_language, summary_threshold,
                      CASE WHEN api_key_nonce IS NOT NULL AND api_key_ciphertext IS NOT NULL
                           THEN 1 ELSE 0 END AS api_key_configured,
                      last_test_status, last_tested_at, failed_count, updated_at
                 FROM llm_settings WHERE singleton = 1""",
        )

    def llm_settings_secret() -> str | None:
        """Read the decrypted API key only for the one-shot test operation."""
        if database is None:
            return None
        for name in ("get_llm_settings_secret", "get_llm_api_key", "get_llm_secret"):
            getter = getattr(database, name, None)
            if not callable(getter):
                continue
            value = getter()
            if inspect.isawaitable(value):
                raise RuntimeError("LLM settings stores must expose a synchronous API adapter")
            return str(value) if value else None
        return None

    def public_llm_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
        value = dict(raw or {})
        return {
            "enabled": bool(value.get("enabled", False)),
            "base_url": str(value.get("base_url") or ""),
            "model": str(value.get("model") or ""),
            "default_language": str(value.get("default_language") or "en_US"),
            "summary_threshold": max(0, int(value["summary_threshold"] if value.get("summary_threshold") is not None else 120)),
            "api_key_configured": bool(value.get("api_key_configured")),
            "last_test_status": str(value.get("last_test_status") or "never"),
            "last_tested_at": value.get("last_tested_at"),
            "failed_count": int(value.get("failed_count") or 0),
            "updated_at": value.get("updated_at"),
        }

    async def persist_llm_settings(values: dict[str, Any]) -> dict[str, Any]:
        if database is None:
            raise HTTPException(status_code=503, detail="LLM settings store is unavailable")
        method = next(
            (getattr(database, name, None) for name in ("update_llm_settings", "set_llm_settings", "save_llm_settings")
             if callable(getattr(database, name, None))),
            None,
        )
        if not callable(method):
            raise HTTPException(status_code=503, detail="LLM settings store is unavailable")
        try:
            result = method(values)
        except TypeError:
            result = method(**values)
        result = await _maybe_await(result)
        return public_llm_settings(dict(result) if isinstance(result, dict) else llm_settings_row())

    def has_bound_private_chat() -> bool:
        if database is None:
            return False
        getter = getattr(database, "get_admin_binding", None)
        if not callable(getter):
            return False
        binding = getter()
        if inspect.isawaitable(binding):
            raise RuntimeError("Admin binding store must provide a synchronous API adapter")
        return bool(binding and binding.get("private_chat_id") is not None)

    def draft_by_id(draft_id: str) -> dict[str, Any] | None:
        if database is None:
            return None
        result = _db_call(database, ("get_draft", "get_draft_by_id"), int(draft_id))
        if result is not None and not inspect.isawaitable(result):
            return dict(result) if result else None
        v2 = _v2_one(database, "SELECT * FROM drafts WHERE id = ?", (int(draft_id),))
        if v2:
            return v2
        return _legacy_one(database, "SELECT * FROM drafts WHERE id = ?", (int(draft_id),))

    def draft_response(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        # V2's schema has no separate revision column.  Its updated_at is an
        # opaque, monotonically advanced ETag for drafts, including after an
        # application restart; legacy stores keep the in-process fallback.
        persisted_version = int(result.get("version") or result.get("updated_at") or 1)
        result["version"] = runtime.draft_versions.get(str(result["id"]), persisted_version)
        return result

    def draft_version(draft: dict[str, Any], if_match: str | None) -> int:
        expected = runtime.draft_versions.get(str(draft["id"]), int(draft.get("version") or draft.get("updated_at") or 1))
        if if_match is None or if_match.strip('"') != str(expected):
            raise HTTPException(status_code=412, detail="Draft version does not match If-Match")
        return expected

    def touch_draft(draft_id: int, expected: int) -> int:
        next_version = max(expected + 1, int(time.time()))
        if database is not None and getattr(database, "db", None) is not None:
            with database.db.transaction(immediate=True) as connection:
                cursor = connection.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (next_version, int(draft_id)))
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=409, detail="Draft could not be updated")
        runtime.draft_versions[str(draft_id)] = next_version
        return next_version

    def public_attachment(row: dict[str, Any], draft_version_value: int) -> dict[str, Any]:
        return {
            "id": row["id"], "file_name": row["file_name"], "mime_type": row.get("mime_type"),
            "size": row.get("size"), "status": row.get("status") or row.get("availability") or "available",
            "draft_version": draft_version_value,
        }

    def attachment_storage(draft_id: int, local_path: str | None = None) -> tuple[Path, Path]:
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        root = (data_root / "attachments" / str(int(draft_id))).resolve()
        candidate = (data_root / local_path).resolve() if local_path else root
        if not candidate.is_relative_to(root):
            raise HTTPException(status_code=409, detail="Attachment storage record is invalid")
        return data_root, candidate

    def normalize_attachment_name(value: str | None) -> str:
        raw = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/")
        name = Path(raw).name.strip().replace("\x00", "")
        if not name or name in {".", ".."} or len(name) > 255:
            raise HTTPException(status_code=422, detail="Attachment file name is invalid")
        return name

    def verification_error(exc: Exception) -> str:
        import imaplib
        import smtplib
        import socket
        import ssl

        if isinstance(exc, (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error, PermissionError)):
            return "authentication"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(exc, (ConnectionError, ConnectionRefusedError, socket.gaierror, ssl.SSLError, OSError)):
            return "connection"
        return "protocol"

    def consume_setup_code(code: str, user_id: int) -> bool:
        if not code:
            return False
        if database is not None:
            method = getattr(database, "consume_setup_code", None)
            if callable(method):
                try:
                    value = method(code=code, telegram_user_id=user_id)
                except TypeError:
                    value = method(code, user_id)
                if inspect.isawaitable(value):
                    raise RuntimeError("Setup-code store must provide synchronous API adapter")
                return bool(value)
        # Environment fallback is intentionally one-time only per process.  A
        # deployment requiring restart resilience must inject a persistent store.
        if active_settings.setup_code and secrets_compare(code, active_settings.setup_code) and code not in runtime.used_setup_codes:
            runtime.used_setup_codes.add(code)
            return True
        return False

    def bind_admin(user_id: int, *, private_chat_id: int) -> None:
        """Bind the verified Mini App user to their Bot private chat.

        Telegram guarantees that a user's private-chat ``chat.id`` equals their
        ``user.id``.  ``chat_instance`` is deliberately never used here: it is a
        callback-query routing identifier, not a chat ID.  A future webhook /start
        association may supply the same value explicitly through the repository.
        """
        if database is None:
            return
        binder = getattr(database, "bind_admin", None)
        if not callable(binder):
            return
        try:
            try:
                result = binder(telegram_user_id=user_id, private_chat_id=private_chat_id)
            except TypeError:
                try:
                    result = binder(user_id, private_chat_id=private_chat_id)
                except TypeError:
                    result = binder(user_id)
            if inspect.isawaitable(result):
                raise RuntimeError("Admin binding store must provide a synchronous API adapter")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Not authorized for this Mini App") from exc
        setter = getattr(database, "set_admin_private_chat", None)
        if callable(setter):
            try:
                result = setter(user_id, private_chat_id)
            except TypeError:
                result = setter(telegram_user_id=user_id, private_chat_id=private_chat_id)
            if inspect.isawaitable(result):
                raise RuntimeError("Admin binding store must provide a synchronous API adapter")
            return
        # Transitional V2Repository versions accept only telegram_user_id.  The
        # update remains scoped to the already-bound, HMAC-verified principal.
        if getattr(database, "db", None) is not None:
            transaction = getattr(database.db, "transaction", None)
            if callable(transaction):
                with transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE admin_binding SET private_chat_id = ?, updated_at = ? "
                        "WHERE singleton = 1 AND telegram_user_id = ?",
                        (int(private_chat_id), int(time.time()), int(user_id)),
                    )

    def trigger_waiting_projection_replay(private_chat_id: int) -> None:
        """Notify whichever v2 runtime owns deferred Telegram projections.

        The notification is optional and duck-typed so older stores continue to
        work.  Persisting the chat binding above is sufficient for poll-based
        workers; newer runtimes can immediately replay through either dependency.
        """
        for target in (database, telegram):
            for name in (
                "replay_pending_projections",
                "replay_waiting_projections",
                "schedule_waiting_projection_replay",
                "release_waiting_projections",
                "on_private_chat_bound",
            ):
                callback = getattr(target, name, None) if target is not None else None
                if not callable(callback):
                    continue
                try:
                    value = callback(private_chat_id=private_chat_id)
                except TypeError:
                    try:
                        value = callback(private_chat_id)
                    except TypeError:
                        value = callback()
                if inspect.isawaitable(value):
                    asyncio.create_task(_maybe_await(value))
                return
        app.state.waiting_projection_replay_allowed_for = int(private_chat_id)

    def claim_webhook_update(update: dict[str, Any]) -> bool:
        """Atomically claim a Telegram update, reclaiming failed/stale work.

        The unique update ID prevents concurrent delivery, not retries: a failed
        handler or a process that died while processing must be allowed to claim
        the same row again.  ``received_at`` doubles as the short processing
        lease timestamp because the current schema has no separate claimed field.
        """
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return True
        now = int(time.time())
        if database is not None and getattr(database, "db", None) is not None:
            transaction = getattr(database.db, "transaction", None)
            if callable(transaction):
                with transaction(immediate=True) as connection:
                    connection.execute(
                        "INSERT OR IGNORE INTO bot_updates(update_id, update_type, payload_json, received_at, status) VALUES (?, ?, ?, ?, 'received')",
                        (
                            update_id,
                            next((key for key in update if key != "update_id"), None),
                            json.dumps(update, separators=(",", ":")),
                            now,
                        ),
                    )
                    row = connection.execute(
                        "SELECT status, received_at FROM bot_updates WHERE update_id = ?", (update_id,)
                    ).fetchone()
                    if row is None or row["status"] == "processed":
                        return False
                    if row["status"] == "processing" and int(row["received_at"]) > now - WEBHOOK_PROCESSING_LEASE_SECONDS:
                        return False
                    connection.execute(
                        "UPDATE bot_updates SET status = 'processing', received_at = ?, processed_at = NULL, error_message = NULL WHERE update_id = ?",
                        (now, update_id),
                    )
                    return True
        prior = runtime.webhook_updates.get(update_id)
        if prior and prior[0] == "processed":
            return False
        if prior and prior[0] == "processing" and prior[1] > now - WEBHOOK_PROCESSING_LEASE_SECONDS:
            return False
        runtime.webhook_updates[update_id] = ("processing", now)
        return True

    def finish_webhook_update(update_id: int, *, success: bool) -> None:
        if database is not None and getattr(database, "db", None) is not None:
            transaction = getattr(database.db, "transaction", None)
            if callable(transaction):
                with transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE bot_updates SET status = ?, processed_at = ?, error_message = ? WHERE update_id = ?",
                        ("processed" if success else "failed", int(time.time()), None if success else "handler_failed", update_id),
                    )
                return
        runtime.webhook_updates[update_id] = ("processed" if success else "failed", int(time.time()))

    async def execute_operation(operation_id: str, method_name: str, *args: Any) -> None:
        operation = runtime.operations[operation_id]
        operation["status"] = "running"
        try:
            method = getattr(telegram, method_name, None) if telegram is not None else None
            if callable(method):
                value = await _maybe_await(method(*args))
                operation["result"] = value if isinstance(value, dict) else {"accepted": bool(value is not False)}
            else:
                operation["result"] = {"accepted": True}
            operation["status"] = "succeeded"
        except Exception:
            # Do not expose transport or credential details to callers.
            operation["status"] = "failed"
            operation["error"] = "Operation failed"

    def create_operation(kind: str, idempotency_key: str, fingerprint: str, method_name: str, *args: Any) -> dict[str, Any]:
        if not idempotency_key.strip():
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        index = (kind, idempotency_key)
        existing = runtime.idempotency.get(index)
        if existing:
            existing_fingerprint, existing_id = existing
            if existing_fingerprint != fingerprint:
                raise HTTPException(status_code=409, detail="Idempotency-Key was used with a different request")
            return runtime.operations[existing_id]
        operation_id = runtime.operation_id()
        operation = {"id": operation_id, "kind": kind, "status": "accepted", "result": None, "error": None}
        runtime.operations[operation_id] = operation
        runtime.idempotency[index] = (fingerprint, operation_id)
        asyncio.create_task(execute_operation(operation_id, method_name, *args))
        return operation

    def durable_operation_view(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
        operation_id = raw.get("id")
        return {
            "id": f"{kind}:{operation_id}",
            "kind": "draft.send" if kind == "send" else "thread.delete",
            "status": str(raw.get("status") or "queued"),
            "result": {"provider_message_id": raw["provider_message_id"]} if raw.get("provider_message_id") else None,
            # Raw worker/provider text must not be reflected through the API.
            "error": str(raw.get("error_code")) if raw.get("error_code") else None,
        }

    def verify_auth_identity(init_data: str):
        try:
            return verify_telegram_init_data(
                init_data,
                bot_token=active_settings.telegram_bot_token,
                max_age_seconds=active_settings.init_data_max_age_seconds,
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail="Telegram authentication failed") from exc

    def require_permitted_identity(user_id: int, *, setup_allowed: bool) -> None:
        if (
            active_settings.admin_telegram_user_id is not None
            and user_id != active_settings.admin_telegram_user_id
        ):
            raise HTTPException(status_code=403, detail="Not authorized for this Mini App")
        bound = get_bound_admin()
        if bound is None:
            if not setup_allowed:
                raise HTTPException(status_code=401, detail="Mini App setup is required")
            return
        if bound != user_id:
            raise HTTPException(status_code=403, detail="Not authorized for this Mini App")
        if not has_bound_private_chat() and not setup_allowed:
            raise HTTPException(status_code=401, detail="Mini App setup is required")

    def establish_session(response: Response, user_id: int) -> dict[str, Any]:
        cookie, session = issue_session(
            user_id=user_id,
            secret=active_settings.session_secret,
            ttl_seconds=active_settings.session_ttl_seconds,
        )
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=active_settings.session_ttl_seconds,
            httponly=True,
            secure=active_settings.secure_cookies,
            # Telegram Web embeds Mini Apps cross-site. Secure production
            # sessions therefore need SameSite=None or the browser accepts the
            # setup response but withholds the cookie from every later API call.
            samesite="none" if active_settings.secure_cookies else "lax",
            path="/",
        )
        return {"authenticated": True, "telegram_user_id": user_id, "csrf_token": session.csrf_token}

    @router.post("/auth/setup", response_model=AuthStatusResponse)
    async def auth_setup(payload: AuthSetupRequest, response: Response) -> dict[str, Any]:
        identity = verify_auth_identity(payload.init_data)
        require_permitted_identity(identity.user_id, setup_allowed=True)
        bound = get_bound_admin()
        if bound is None:
            if not consume_setup_code(payload.code, identity.user_id):
                raise HTTPException(status_code=401, detail="Setup code is invalid or already used")
            # initData carries no chat id.  This uses the documented private-chat
            # identity relation after HMAC verification, never chat_instance.
            bind_admin(identity.user_id, private_chat_id=identity.user_id)
            trigger_waiting_projection_replay(identity.user_id)
        elif not has_bound_private_chat():
            # A /start-created binding can predate Mini App setup.  It is already
            # the same verified admin, so complete its private-chat association
            # without consuming the single-use bootstrap code a second time.
            bind_admin(identity.user_id, private_chat_id=identity.user_id)
            trigger_waiting_projection_replay(identity.user_id)
        return establish_session(response, identity.user_id)

    @router.post("/auth/session", response_model=AuthStatusResponse)
    async def auth_session(payload: AuthSessionRequest, response: Response) -> dict[str, Any]:
        """Issue a fresh session for the already-bound Telegram administrator."""
        identity = verify_auth_identity(payload.init_data)
        require_permitted_identity(identity.user_id, setup_allowed=False)
        return establish_session(response, identity.user_id)

    @router.get("/auth/status", response_model=AuthStatusResponse)
    async def auth_status(request: Request) -> dict[str, Any]:
        session = read_session(request.cookies.get(SESSION_COOKIE), secret=active_settings.session_secret)
        bound = get_bound_admin()
        allowed = bool(session) and (
            active_settings.admin_telegram_user_id is None or session.user_id == active_settings.admin_telegram_user_id
        ) and (bound is None or session.user_id == bound)
        return {
            "authenticated": allowed,
            "telegram_user_id": session.user_id if allowed and session else None,
            "csrf_token": session.csrf_token if allowed and session else None,
        }

    @router.get("/settings/llm", response_model=LLMSettingsResponse)
    async def get_llm_settings(_: Session = Depends(require_session)) -> dict[str, Any]:
        if database is None:
            raise HTTPException(status_code=503, detail="LLM settings store is unavailable")
        return public_llm_settings(llm_settings_row())

    @router.put("/settings/llm", response_model=LLMSettingsResponse)
    async def put_llm_settings(
        payload: LLMSettingsInput,
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        values = _dump(payload, exclude_unset=True)
        for key in ("base_url", "model"):
            if key in values and values[key] is not None:
                values[key] = str(values[key]).strip()
        # A blank password field means "keep the encrypted key"; omitting it
        # entirely prevents lower-level duck-typed stores from clearing it.
        if not str(values.get("api_key") or "").strip():
            values.pop("api_key", None)
        try:
            return await persist_llm_settings(values)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/settings/llm/test", response_model=LLMTestResponse)
    async def test_llm_settings(
        payload: LLMSettingsInput | None = None,
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        supplied = _dump(payload, exclude_unset=True) if payload is not None else {}
        stored = public_llm_settings(llm_settings_row())
        base_url = str(supplied.get("base_url") if supplied.get("base_url") is not None else stored["base_url"]).strip()
        model = str(supplied.get("model") if supplied.get("model") is not None else stored["model"]).strip()
        tested_at = int(time.time())

        async def record(result: str) -> None:
            recorder = getattr(database, "record_llm_test", None) if database is not None else None
            if not callable(recorder):
                return
            try:
                value = recorder(result, tested_at=tested_at)
            except TypeError:
                value = recorder(status=result)
            await _maybe_await(value)

        try:
            api_key = str(supplied.get("api_key") or "").strip() or llm_settings_secret()
        except Exception:
            await record("failed")
            return {
                "ok": False,
                "status": "failed",
                "last_test_status": "failed",
                "last_tested_at": tested_at,
                "error": "configuration",
            }

        if not base_url or not model or not api_key:
            await record("failed")
            return {
                "ok": False,
                "status": "failed",
                "last_test_status": "failed",
                "last_tested_at": tested_at,
                "error": "configuration",
            }

        def probe() -> None:
            # The shortest provider-neutral OpenAI-compatible stream verifies
            # endpoint, credential, model access and streaming support at once.
            from openai import OpenAI

            client = OpenAI(base_url=base_url, api_key=api_key, timeout=15.0, max_retries=0)
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply OK."}],
                stream=True,
                max_tokens=1,
            )
            try:
                next(iter(stream), None)
            finally:
                closer = getattr(stream, "close", None)
                if callable(closer):
                    closer()

        try:
            await asyncio.to_thread(probe)
        except Exception:
            # Provider errors can contain request URLs, headers or key fragments;
            # return only a stable category across this security boundary.
            await record("failed")
            return {
                "ok": False,
                "status": "failed",
                "last_test_status": "failed",
                "last_tested_at": tested_at,
                "error": "connection",
            }
        await record("ok")
        return {
            "ok": True,
            "status": "ok",
            "last_test_status": "ok",
            "last_tested_at": tested_at,
            "error": None,
        }

    @router.get("/settings/llm/status")
    async def get_llm_summary_status(_: Session = Depends(require_session)) -> dict[str, Any]:
        """Return configuration health plus compact durable queue counts."""
        result: dict[str, Any] = public_llm_settings(llm_settings_row())
        tasks = getattr(database, "list_summary_tasks", None) if database is not None else None
        counts = {name: 0 for name in ("pending", "queued", "running", "completed", "succeeded", "failed", "skipped")}
        if callable(tasks):
            rows = await _maybe_await(tasks(limit=500))
            for row in rows or []:
                state = str(dict(row).get("status") or "")
                if state in counts:
                    counts[state] += 1
        result["summary_tasks"] = counts
        return result

    @router.post("/emails/{email_id}/summary/retry")
    async def retry_email_summary(email_id: int, _: Session = Depends(require_csrf)) -> dict[str, Any]:
        if database is None:
            raise HTTPException(status_code=503, detail="Summary task store is unavailable")
        method = next(
            (getattr(database, name, None) for name in ("retry_email_summary", "enqueue_summary_task", "create_summary_task")
             if callable(getattr(database, name, None))),
            None,
        )
        if not callable(method):
            raise HTTPException(status_code=503, detail="Summary task store is unavailable")
        try:
            result = method(email_id, force=True)
        except TypeError:
            result = method(email_id)
        try:
            task = await _maybe_await(result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Email not found") from exc
        if not task:
            raise HTTPException(status_code=404, detail="Email not found")
        worker_runtime = getattr(app.state, "worker_runtime", None)
        wake_workers = getattr(worker_runtime, "wake", None)
        if callable(wake_workers):
            wake_workers()
        return dict(task)

    @router.get("/accounts", response_model=list[AccountResponse])
    async def list_accounts(_: Session = Depends(require_session)) -> list[dict[str, Any]]:
        return [safe_account(row) for row in account_rows()]

    @router.post("/accounts", response_model=AccountResponse, status_code=201)
    async def create_account(payload: AccountInput, _: Session = Depends(require_csrf)) -> dict[str, Any]:
        if database is None:
            raise HTTPException(status_code=503, detail="Account store is unavailable")
        account = _dump(payload)
        _validate_account_transport(account)
        account["imap_ssl"] = int(bool(account["imap_ssl"]))
        account["smtp_ssl"] = int(bool(account["smtp_ssl"]))
        # A freshly saved credential must never be polled before both transports
        # have completed the explicit authenticated verification route.
        account["enabled"] = False
        creator = getattr(database, "create_account", None)
        if callable(creator):
            password = account.pop("password", None)
            if password is None:
                raise HTTPException(status_code=422, detail="password is required when creating an account")
            try:
                result = creator(account, password)
            except RuntimeError as exc:
                if str(exc) == "account cleanup is still in progress":
                    raise HTTPException(status_code=409, detail="Account cleanup is still in progress") from exc
                raise
        else:
            legacy_creator = getattr(database, "add_account", None)
            if not callable(legacy_creator):
                raise HTTPException(status_code=503, detail="Account store is unavailable")
            result = legacy_creator(account)
        result = await _maybe_await(result)
        if result is False or result is None:
            raise HTTPException(status_code=409, detail="Account could not be created")
        created_id = result.get("id") if isinstance(result, dict) else None
        if created_id is not None:
            marker = getattr(database, "mark_account_verification_pending", None)
            if callable(marker):
                await _maybe_await(marker(int(created_id)))
        worker_runtime = getattr(app.state, "worker_runtime", None)
        if callable(getattr(worker_runtime, "wake", None)):
            worker_runtime.wake()
        if isinstance(result, dict):
            return safe_account(result)
        created = next((item for item in account_rows() if item.get("email") == account["email"]), account)
        return safe_account(created)

    @router.get("/accounts/{account_id}", response_model=AccountResponse)
    async def get_account(account_id: int, _: Session = Depends(require_session)) -> dict[str, Any]:
        row = account_by_id(account_id)
        if not row:
            raise HTTPException(status_code=404, detail="Account not found")
        return safe_account(row)

    @router.patch("/accounts/{account_id}", response_model=AccountResponse)
    async def patch_account(account_id: int, payload: AccountPatchRequest, _: Session = Depends(require_csrf)) -> dict[str, Any]:
        current = account_by_id(account_id)
        if database is None or not current:
            raise HTTPException(status_code=404, detail="Account not found")
        updates = _dump(payload, exclude_unset=True)
        password = updates.pop("password", None)
        transport_keys = {"imap_server", "imap_port", "imap_ssl", "smtp_server", "smtp_port", "smtp_ssl"}
        if transport_keys.intersection(updates) or password is not None:
            _validate_account_transport({**current, **updates})
            # Any endpoint/password change invalidates the previous verification.
            updates["enabled"] = False
        for key in ("imap_ssl", "smtp_ssl"):
            if key in updates:
                updates[key] = int(bool(updates[key]))
        method = getattr(database, "update_account", None)
        if not callable(method):
            raise HTTPException(status_code=503, detail="Account store is unavailable")
        try:
            result = method(updates, id=account_id)
        except TypeError:
            result = method(account_id, updates)
        if not await _maybe_await(result):
            raise HTTPException(status_code=409, detail="Account could not be updated")
        if password is not None:
            set_password = getattr(database, "set_account_password", None)
            if callable(set_password):
                await _maybe_await(set_password(account_id, password))
            elif "password" not in updates:
                # The legacy account manager accepts a password in its normal
                # update dict; v2 uses its encrypted dedicated method above.
                legacy_update = {"password": password}
                try:
                    await _maybe_await(method(legacy_update, id=account_id))
                except TypeError:
                    raise HTTPException(status_code=503, detail="Credential store is unavailable")
        if transport_keys.intersection(updates) or password is not None:
            marker = getattr(database, "mark_account_verification_pending", None)
            if callable(marker):
                await _maybe_await(marker(int(account_id)))
            worker_runtime = getattr(app.state, "worker_runtime", None)
            if callable(getattr(worker_runtime, "wake", None)):
                worker_runtime.wake()
        return safe_account(account_by_id(account_id) or {"id": account_id, **updates})

    @router.delete("/accounts/{account_id}", response_model=AccountDeleteResponse)
    async def delete_account(account_id: int, payload: AccountDeleteRequest | None = None, _: Session = Depends(require_csrf)) -> dict[str, Any]:
        if database is None or not account_by_id(account_id):
            raise HTTPException(status_code=404, detail="Account not found")
        purge_data = bool(payload.purge_data) if payload is not None else False
        if purge_data:
            creator = getattr(database, "create_account_delete_operation", None)
            if not callable(creator):
                raise HTTPException(status_code=503, detail="Account deletion store is unavailable")
            try:
                operation = await _maybe_await(creator(int(account_id), purge_data=True))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Account not found") from exc
            # A pending purge must never be picked up by ingestion again.
            soft_delete = getattr(database, "soft_delete_account", None)
            if callable(soft_delete):
                await _maybe_await(soft_delete(int(account_id)))
            worker_runtime = getattr(app.state, "worker_runtime", None)
            if callable(getattr(worker_runtime, "wake", None)):
                worker_runtime.wake()
            return {"id": str(operation["id"]), "status": operation.get("status", "queued"), "purge_data": True}
        soft_delete = getattr(database, "soft_delete_account", None)
        if not callable(soft_delete):
            raise HTTPException(status_code=503, detail="Account store is unavailable")
        if not await _maybe_await(soft_delete(int(account_id))):
            raise HTTPException(status_code=409, detail="Account could not be deleted")
        return {"id": str(account_id), "status": "deleted", "purge_data": False}

    @router.post("/accounts/{account_id}/verify", response_model=AccountVerifyResponse)
    async def verify_account(account_id: int, _: Session = Depends(require_csrf)) -> dict[str, Any]:
        account = account_by_id(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        password_getter = getattr(database, "get_account_password", None) if database is not None else None
        if not callable(password_getter):
            raise HTTPException(status_code=503, detail="Encrypted credential store is unavailable")
        try:
            password = await asyncio.to_thread(password_getter, int(account_id))
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="Account credential is not configured") from exc
        transport_account = {**account, "password": password}
        if imap_transport_factory is None:
            from app.integrations.mail.imap import IMAPTransport

            imap_transport = IMAPTransport(transport_account)
        else:
            imap_transport = imap_transport_factory(transport_account)
        if smtp_transport_factory is None:
            from app.integrations.mail.smtp import SMTPTransport

            smtp_transport = SMTPTransport(transport_account)
        else:
            smtp_transport = smtp_transport_factory(transport_account)

        async def check(transport: Any) -> dict[str, Any]:
            try:
                result = await _maybe_await(transport.verify())
                if result is False:
                    return {"ok": False, "error": "connection"}
                return {"ok": True, "error": None}
            except Exception as exc:
                return {"ok": False, "error": verification_error(exc)}

        imap, smtp = await asyncio.gather(check(imap_transport), check(smtp_transport))
        recorder = getattr(database, "record_account_verification", None) if database is not None else None
        if callable(recorder):
            await _maybe_await(recorder(int(account_id), ok=bool(imap["ok"] and smtp["ok"]), error=";".join(
                str(value["error"]) for value in (imap, smtp) if not value["ok"] and value.get("error")
            ) or None))
            return {"imap": imap, "smtp": smtp}
        updater = getattr(database, "update_account", None) if database is not None else None
        if callable(updater):
            enabled = bool(imap["ok"] and smtp["ok"])
            try:
                changed = updater(int(account_id), {"enabled": enabled})
            except TypeError:
                changed = updater({"enabled": enabled}, id=int(account_id))
            if not await _maybe_await(changed):
                raise HTTPException(status_code=409, detail="Account verification state could not be updated")
        return {"imap": imap, "smtp": smtp}

    @router.get("/contacts", response_model=list[ContactResponse])
    async def contacts(q: str = "", limit: int = 20, account_id: int | None = None, _: Session = Depends(require_session)) -> list[dict[str, Any]]:
        bounded_limit = min(max(limit, 1), 100)
        result: Any = None
        search = getattr(database, "search_contacts", None) if database is not None else None
        if callable(search):
            account_ids = [account_id] if account_id is not None else [int(row["id"]) for row in account_rows() if row.get("id") is not None]
            result = []
            for candidate_account_id in account_ids:
                try:
                    result.extend(await _maybe_await(search(candidate_account_id, q, limit=bounded_limit)))
                except TypeError:
                    result.extend(await _maybe_await(search(q=q, limit=bounded_limit)))
        elif database:
            result = _db_call(database, ("list_contacts",), q=q, limit=bounded_limit)
            result = await _maybe_await(result)
        if result is None:
            rows = _legacy_rows(
                database,
                "SELECT DISTINCT sender AS email FROM emails WHERE sender LIKE ? AND sender <> '' LIMIT ?",
                (f"%{q}%", bounded_limit),
            ) if database else []
            result = rows
        contacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in result or []:
            value = dict(raw)
            email = str(value.get("email") or value.get("sender") or "").strip()
            if email and email.lower() not in seen:
                seen.add(email.lower())
                contacts.append({"email": email, "name": value.get("name") or value.get("display_name") or None})
        return contacts[:bounded_limit]

    @router.get("/threads", response_model=list[ThreadResponse])
    async def threads(limit: int = 50, _: Session = Depends(require_session)) -> list[dict[str, Any]]:
        # Mini App remains a bounded operation console, not a full historical
        # mailbox browser.  Telegram owns deep reading and cursor navigation.
        bounded_limit = min(max(limit, 1), 50)
        result = _db_call(database, ("list_threads",), limit=bounded_limit) if database else None
        result = await _maybe_await(result)
        # The v2 repository owns the materialized latest-email projection, so a
        # Mini App refresh does not run one correlated latest-message scan per
        # thread.  Keep the legacy fallback below for old adapters.
        if result is None:
            if database and getattr(database, "db", None) is not None:
                result = _v2_rows(
                    database,
                    """SELECT t.id, t.account_id, e.subject,
                       e.email_date AS latest_at, COUNT(all_e.id) AS message_count,
                       e.llm_summary AS summary, e.summary_status AS summary_status,
                       e.llm_priority AS priority, e.llm_category AS category,
                       e.summary_updated_at
                       FROM mail_threads t
                       JOIN emails e ON e.id = t.latest_email_id AND e.tombstoned_at IS NULL
                       LEFT JOIN emails all_e ON all_e.thread_id = t.id AND all_e.tombstoned_at IS NULL
                       WHERE t.status = 'active'
                         AND NOT EXISTS (
                           SELECT 1 FROM delete_operations d
                           WHERE d.thread_id = t.id AND d.status IN ('queued', 'deleting', 'failed')
                         )
                       GROUP BY t.id, t.account_id, e.id
                       ORDER BY t.latest_at DESC, t.id DESC LIMIT ?""",
                    (bounded_limit,),
                )
            else:
                result = _legacy_rows(
                    database,
                    """SELECT telegram_thread_id AS id, email_account AS account_id, MAX(subject) AS subject,
                       MAX(email_date) AS latest_at, COUNT(*) AS message_count FROM emails
                       WHERE COALESCE(telegram_thread_id, '') <> '' GROUP BY telegram_thread_id, email_account
                       ORDER BY latest_at DESC LIMIT ?""",
                    (bounded_limit,),
                ) if database else []
        normalized_threads: list[dict[str, Any]] = []
        for item in result or []:
            value = dict(item)
            if value.get("status") == "tombstoned":
                continue
            value["id"] = str(value.get("id"))
            value["summary"] = value.get("summary", value.get("llm_summary"))
            value["summary_status"] = value.get("summary_status")
            value["priority"] = value.get("priority", value.get("llm_priority"))
            value["category"] = value.get("category", value.get("llm_category"))
            value["summary_updated_at"] = value.get("summary_updated_at")
            normalized_threads.append(value)
        return normalized_threads

    @router.get("/emails/{email_id}/inline-assets/{asset_id}", include_in_schema=False)
    async def inline_asset(email_id: int, asset_id: int, _: Session = Depends(require_session)) -> Response:
        if database is None or email_id <= 0 or asset_id <= 0:
            raise HTTPException(status_code=404, detail="Inline asset not found")
        asset = await _maybe_await(
            _db_call(database, ("get_email_inline_asset",), int(email_id), int(asset_id))
        )
        if asset is None and getattr(database, "db", None) is not None:
            asset = _v2_one(
                database,
                """SELECT a.* FROM email_inline_assets a
                   JOIN emails e ON e.id = a.email_id
                   WHERE a.email_id = ? AND a.id = ? AND e.tombstoned_at IS NULL""",
                (int(email_id), int(asset_id)),
            )
        if not asset:
            raise HTTPException(status_code=404, detail="Inline asset not found")
        mime_type = str(asset.get("mime_type") or "").split(";", 1)[0].strip().lower()
        if mime_type not in INLINE_ASSET_MIME_TYPES:
            raise HTTPException(status_code=404, detail="Inline asset not found")
        local_path = asset.get("local_path")
        if not local_path:
            raise HTTPException(status_code=404, detail="Inline asset not found")
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        asset_root = (data_root / "inline-assets" / str(int(email_id))).resolve()
        candidate = (data_root / str(local_path)).resolve()
        if not candidate.is_relative_to(asset_root) or not candidate.is_file():
            raise HTTPException(status_code=404, detail="Inline asset not found")
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="Inline asset not found") from exc
        expected_size = int(asset.get("size") or -1)
        expected_hash = str(asset.get("sha256") or "").lower()
        if expected_size != len(payload) or len(payload) > 10 * 1024 * 1024 or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise HTTPException(status_code=404, detail="Inline asset not found")
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise HTTPException(status_code=404, detail="Inline asset not found")
        return FileResponse(
            candidate,
            media_type=mime_type,
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/threads/{thread_id}/messages", response_model=list[MessageResponse])
    async def messages(thread_id: str, limit: int = 100, _: Session = Depends(require_session)) -> list[dict[str, Any]]:
        bounded_limit = min(max(limit, 1), 200)
        if database and getattr(database, "db", None) is not None:
            try:
                internal_thread_id = int(thread_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Thread not found") from exc
            thread = _v2_one(
                database,
                "SELECT id FROM mail_threads WHERE id = ? AND status <> 'tombstoned'",
                (internal_thread_id,),
            )
            if thread is None:
                raise HTTPException(status_code=404, detail="Thread not found")
        result = _db_call(database, ("list_thread_messages", "list_messages"), thread_id=thread_id, limit=bounded_limit) if database else None
        result = await _maybe_await(result)
        if result is None:
            if database and getattr(database, "db", None) is not None:
                result = _v2_rows(
                    database,
                    """SELECT id, thread_id, account_id, sender, recipient, cc, subject, body_text, body_html, email_date,
                              llm_summary AS summary, summary_status, llm_priority AS priority,
                              llm_category AS category, summary_updated_at
                       FROM emails WHERE thread_id = ? AND tombstoned_at IS NULL ORDER BY id ASC LIMIT ?""",
                    (int(thread_id), bounded_limit),
                )
            else:
                result = _legacy_rows(
                    database,
                    """SELECT id, telegram_thread_id AS thread_id, email_account AS account_id, sender, recipient, cc,
                       subject, body_text, body_html, email_date FROM emails WHERE telegram_thread_id = ?
                       ORDER BY id ASC LIMIT ?""",
                    (thread_id, bounded_limit),
                ) if database else []
        normalized_messages: list[dict[str, Any]] = []
        for item in result or []:
            value = dict(item)
            if value.get("tombstoned_at") is not None:
                continue
            value["thread_id"] = str(value["thread_id"]) if value.get("thread_id") is not None else None
            value["summary"] = value.get("summary", value.get("llm_summary"))
            value["priority"] = value.get("priority", value.get("llm_priority"))
            value["category"] = value.get("category", value.get("llm_category"))
            message_id = value.get("id")
            inline_assets: Any = []
            if message_id is not None and database is not None:
                try:
                    inline_assets = await _maybe_await(
                        _db_call(database, ("list_email_inline_assets",), int(message_id))
                    )
                except (TypeError, ValueError):
                    inline_assets = []
                if inline_assets is None and getattr(database, "db", None) is not None:
                    inline_assets = _v2_rows(
                        database,
                        """SELECT id, content_id, mime_type, size FROM email_inline_assets
                           WHERE email_id = ? ORDER BY id""",
                        (int(message_id),),
                    )
            value["inline_assets"] = [
                {
                    "id": item.get("id"),
                    "content_id": str(item.get("content_id") or ""),
                    "mime_type": str(item.get("mime_type") or ""),
                    "size": int(item.get("size") or 0),
                }
                for item in (inline_assets or [])
                if item.get("id") is not None and item.get("content_id")
            ]
            normalized_messages.append(value)
        return normalized_messages

    @router.post("/drafts", response_model=DraftResponse, status_code=201)
    async def create_draft(payload: DraftCreateRequest, _: Session = Depends(require_csrf)) -> dict[str, Any]:
        if database is None:
            raise HTTPException(status_code=503, detail="Draft store is unavailable")
        values = _dump(payload)
        method = getattr(database, "create_draft", None)
        if not callable(method):
            raise HTTPException(status_code=503, detail="Draft store is unavailable")
        try:
            created = await _maybe_await(method(
                values["account_id"], draft_type=values["draft_type"], thread_id=values["thread_id"] or None,
                from_identity_email=values["from_identity_email"] or None, subject=values.get("subject"),
                body_markdown=values.get("body_markdown"),
            ))
            draft_id = created.get("id") if isinstance(created, dict) else created
        except TypeError:
            draft_id = await _maybe_await(method(**{key: values[key] for key in ("account_id", "chat_id", "thread_id", "draft_type", "from_identity_email")}))
        if not draft_id:
            raise HTTPException(status_code=409, detail="Draft could not be created")
        updates = {key: values[key] for key in ("to_addrs", "cc_addrs", "bcc_addrs", "subject", "body_markdown") if values.get(key) is not None}
        if updates:
            update = getattr(database, "update_draft", None)
            if callable(update):
                await _maybe_await(update(draft_id=int(draft_id), updates=updates))
        created_row = draft_by_id(str(draft_id)) or {"id": draft_id, **values, "status": "open"}
        runtime.draft_versions[str(draft_id)] = int(created_row.get("version") or created_row.get("updated_at") or 1)
        return draft_response(created_row)

    @router.patch("/drafts/{draft_id}", response_model=DraftResponse)
    async def patch_draft(
        draft_id: str,
        payload: DraftPatchRequest,
        if_match: str | None = Header(default=None, alias="If-Match"),
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        draft = draft_by_id(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        expected = runtime.draft_versions.get(draft_id, int(draft.get("version") or draft.get("updated_at") or 1))
        if if_match is None or if_match.strip('"') != str(expected):
            raise HTTPException(status_code=412, detail="Draft version does not match If-Match")
        updates = {key: value for key, value in _dump(payload, exclude_unset=True).items() if value is not None}
        next_version = max(expected + 1, int(time.time()))
        if updates:
            update = getattr(database, "update_draft", None) if database is not None else None
            if callable(update):
                if not await _maybe_await(update(draft_id=int(draft_id), updates={**updates, "_updated_at": next_version})):
                    raise HTTPException(status_code=409, detail="Draft could not be updated")
            elif getattr(database, "db", None) is not None:
                allowed = {key: value for key, value in updates.items() if key in {"from_identity_email", "subject", "body_markdown"}}
                if allowed:
                    allowed["updated_at"] = next_version
                    transaction = database.db.transaction
                    with transaction(immediate=True) as connection:
                        assignments = ", ".join(f"{key} = ?" for key in allowed)
                        cursor = connection.execute(f"UPDATE drafts SET {assignments} WHERE id = ?", tuple(allowed.values()) + (int(draft_id),))
                        if cursor.rowcount != 1:
                            raise HTTPException(status_code=409, detail="Draft could not be updated")
            else:
                raise HTTPException(status_code=503, detail="Draft store is unavailable")
        runtime.draft_versions[draft_id] = next_version
        return draft_response(draft_by_id(draft_id) or {**draft, **updates})

    @router.post("/drafts/{draft_id}/attachments", response_model=AttachmentResponse, status_code=201)
    async def upload_draft_attachment(
        draft_id: str,
        file: UploadFile = File(...),
        if_match: str | None = Header(default=None, alias="If-Match"),
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        draft = draft_by_id(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if getattr(database, "db", None) is None:
            raise HTTPException(status_code=503, detail="Draft attachment storage is unavailable")
        expected = draft_version(draft, if_match)
        name = normalize_attachment_name(file.filename)
        content = await file.read(25 * 1024 * 1024 + 1)
        if len(content) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Attachment exceeds the 25 MiB limit")
        data_root, directory = attachment_storage(int(draft_id))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        physical_name = uuid.uuid4().hex
        destination = (directory / physical_name).resolve()
        if not destination.is_relative_to(directory):  # defensive even though the name is generated here
            raise HTTPException(status_code=409, detail="Attachment storage record is invalid")
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
            os.chmod(destination, 0o600)
            relative_path = str(destination.relative_to(data_root))
            adder = getattr(database, "add_draft_attachment", None)
            if not callable(adder):
                raise HTTPException(status_code=503, detail="Draft attachment store is unavailable")
            created = await _maybe_await(adder(
                int(draft_id), file_name=name, mime_type=file.content_type or "application/octet-stream", size=len(content),
                local_path=relative_path, status="available", sha256=hashlib.sha256(content).hexdigest(),
            ))
        except HTTPException:
            destination.unlink(missing_ok=True)
            raise
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail="Attachment could not be saved") from exc
        return public_attachment(dict(created), touch_draft(int(draft_id), expected))

    @router.delete("/drafts/{draft_id}/attachments/{attachment_id}", response_model=AttachmentResponse)
    async def delete_draft_attachment(
        draft_id: str,
        attachment_id: int,
        if_match: str | None = Header(default=None, alias="If-Match"),
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        draft = draft_by_id(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        expected = draft_version(draft, if_match)
        getter = getattr(database, "get_draft_attachment", None) if database is not None else None
        deleter = getattr(database, "delete_draft_attachment", None) if database is not None else None
        if not callable(getter) or not callable(deleter):
            raise HTTPException(status_code=503, detail="Draft attachment store is unavailable")
        attachment = await _maybe_await(getter(int(draft_id), attachment_id))
        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found")
        local_path = attachment.get("local_path")
        if local_path:
            _data_root, path = attachment_storage(int(draft_id), str(local_path))
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(status_code=409, detail="Attachment could not be removed") from exc
        if not await _maybe_await(deleter(int(draft_id), attachment_id)):
            raise HTTPException(status_code=409, detail="Attachment could not be removed")
        return public_attachment(dict(attachment), touch_draft(int(draft_id), expected))

    @router.post("/drafts/{draft_id}/send", response_model=OperationResponse, status_code=202)
    async def send_draft(
        draft_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        draft = draft_by_id(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        durable_create = getattr(database, "create_send_operation", None) if database is not None else None
        if callable(durable_create):
            if not idempotency_key or not idempotency_key.strip():
                raise HTTPException(status_code=400, detail="Idempotency-Key is required")
            operation = await _maybe_await(durable_create(int(draft["account_id"]), idempotency_key, draft_id=int(draft_id)))
            if operation.get("draft_id") not in (None, int(draft_id)):
                raise HTTPException(status_code=409, detail="Idempotency-Key was used with a different request")
            return durable_operation_view("send", operation)
        return create_operation("draft.send", idempotency_key or "", draft_id, "send_draft", draft)

    @router.get("/operations/{operation_id}", response_model=OperationResponse)
    async def get_operation(operation_id: str, _: Session = Depends(require_session)) -> dict[str, Any]:
        if ":" in operation_id and database is not None:
            kind, raw_id = operation_id.split(":", 1)
            getter = getattr(database, "get_operation", None)
            if kind in {"send", "delete"} and callable(getter) and raw_id.isdigit():
                operation = await _maybe_await(getter(kind, int(raw_id)))
                if operation:
                    return durable_operation_view(kind, operation)
        operation = runtime.operations.get(operation_id)
        if not operation:
            raise HTTPException(status_code=404, detail="Operation not found")
        return operation

    async def queue_thread_delete(thread_id: str, idempotency_key: str | None) -> dict[str, Any]:
        durable_create = (
            getattr(database, "create_thread_delete_operation", None)
            or getattr(database, "create_delete_operation", None)
        ) if database is not None else None
        if callable(durable_create):
            if not idempotency_key or not idempotency_key.strip():
                raise HTTPException(status_code=400, detail="Idempotency-Key is required")
            try:
                internal_thread_id = int(thread_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Thread not found") from exc
            existing = _v2_one(
                database,
                "SELECT * FROM delete_operations WHERE thread_id = ? AND idempotency_key = ?",
                (internal_thread_id, idempotency_key.strip()),
            )
            if existing:
                return durable_operation_view("delete", existing)
            thread = _v2_one(
                database,
                "SELECT id, account_id FROM mail_threads WHERE id = ? AND status <> 'tombstoned'",
                (internal_thread_id,),
            )
            if not thread:
                raise HTTPException(status_code=404, detail="Thread not found")
            try:
                # The repository creates its operation and snapshots every
                # non-tombstoned message mapping in one transaction.  Do not
                # derive a delete target from a first arbitrary email here.
                operation = await _maybe_await(
                    durable_create(int(thread["account_id"]), idempotency_key, thread_id=internal_thread_id)
                )
            except TypeError as exc:
                raise HTTPException(status_code=503, detail="Thread delete store is unavailable") from exc
            operation_thread_id = operation.get("thread_id")
            if operation_thread_id not in (None, internal_thread_id):
                raise HTTPException(status_code=409, detail="Idempotency-Key was used with a different request")
            return durable_operation_view("delete", operation)
        return create_operation("thread.delete", idempotency_key or "", thread_id, "delete_thread", thread_id)

    def wake_worker_runtime() -> None:
        worker_runtime = getattr(app.state, "worker_runtime", None)
        wake_workers = getattr(worker_runtime, "wake", None)
        if callable(wake_workers):
            wake_workers()

    @router.post("/threads/bulk-delete", response_model=BulkDeleteResponse, status_code=202)
    async def bulk_delete_threads(
        payload: BulkDeleteRequest,
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        accepted = False
        try:
            for item in payload.items:
                try:
                    operation = await queue_thread_delete(item.thread_id, item.idempotency_key)
                except HTTPException as exc:
                    results.append({
                        "thread_id": item.thread_id,
                        "id": "",
                        "kind": "thread.delete",
                        "status": "failed",
                        "result": None,
                        "error": str(exc.detail),
                    })
                else:
                    accepted = True
                    results.append({"thread_id": item.thread_id, **operation})
        finally:
            if accepted:
                wake_worker_runtime()
        return {"items": results}

    @router.delete("/threads/{thread_id}", response_model=OperationResponse, status_code=202)
    async def delete_thread(
        thread_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        _: Session = Depends(require_csrf),
    ) -> dict[str, Any]:
        operation = await queue_thread_delete(thread_id, idempotency_key)
        wake_worker_runtime()
        return operation

    @router.post("/telegram/webhook", status_code=204, include_in_schema=False)
    async def telegram_webhook(
        request: Request,
        secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> Response:
        if not verify_webhook_secret(
            {"X-Telegram-Bot-Api-Secret-Token": secret_token or ""}, active_settings.webhook_secret
        ):
            raise HTTPException(status_code=403, detail="Webhook secret is invalid")
        try:
            update = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Webhook update must be JSON") from exc
        if not isinstance(update, dict):
            raise HTTPException(status_code=400, detail="Webhook update must be an object")
        update_id = update.get("update_id")
        if not claim_webhook_update(update):
            return Response(status_code=204)
        handler = getattr(telegram, "handle_update", None) if telegram is not None else None
        if not callable(handler):
            if isinstance(update_id, int):
                finish_webhook_update(update_id, success=False)
            raise HTTPException(status_code=503, detail="Telegram dispatcher is unavailable")
        try:
            handled = await _maybe_await(handler(update))
            if handled is False:
                raise RuntimeError("Telegram dispatcher did not handle update")
        except Exception as exc:
            if isinstance(update_id, int):
                finish_webhook_update(update_id, success=False)
            # Telegram retries non-2xx webhooks.  Do not expose update contents
            # or transport details in the response.
            logger.exception(
                "Telegram update handling failed: update_id=%s error_type=%s",
                update_id,
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="Telegram update handling failed") from exc
        if isinstance(update_id, int):
            finish_webhook_update(update_id, success=True)
        return Response(status_code=204)

    app.include_router(router, prefix=API_PREFIX)
    # Keep the initially published v2 paths alive during the client/entrypoint
    # migration.  Both prefixes intentionally expose the exact same handlers.
    app.include_router(router, prefix=V2_COMPAT_PREFIX, include_in_schema=False)

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        if database is None:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ok"})

    # A mounted distribution always wins; the SPA fallback is intentionally
    # registered last so misspelled API routes remain JSON 404 responses.
    dist = active_settings.web_dist
    if dist and dist.is_dir():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets") if (dist / "assets").is_dir() else None

        @app.get("/{spa_path:path}", include_in_schema=False)
        async def spa(spa_path: str) -> Response:
            if spa_path.startswith("api/") or spa_path.startswith("health/"):
                raise HTTPException(status_code=404, detail="Not found")
            candidate = (dist / spa_path).resolve()
            if spa_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            index = dist / "index.html"
            return FileResponse(index) if index.is_file() else Response(status_code=404)

    return app


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time comparison kept here so route code remains readable."""
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
