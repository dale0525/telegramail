"""Thread-safe SQLite v2 repository used by FastAPI handlers and workers."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
import os
import base64
import json
import re
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence
from urllib.parse import urlsplit

from .crypto import decrypt_secret, encrypt_secret, load_master_key
from .schema import CURRENT_SCHEMA_VERSION, initialize_schema

_UNSET = object()

def _now() -> int:
    return int(time.time())


def _cleanup_email_file_root(data_root: Path, directory: str, email_id: int) -> None:
    """Remove files only from one validated email-specific data directory."""

    if directory not in {"email-attachments", "inline-assets"}:
        raise ValueError("unsupported email file directory")
    parent = (data_root / directory).resolve()
    root = (parent / str(int(email_id))).resolve()
    if not root.is_relative_to(parent) or not root.is_dir():
        return
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        try:
            candidate = child.resolve()
            if candidate.is_relative_to(root) and (child.is_file() or child.is_symlink()):
                child.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _cleanup_email_attachment_root(data_root: Path, email_id: int) -> None:
    _cleanup_email_file_root(data_root, "email-attachments", email_id)


# Failed provider/Telegram deletions are retried by the background worker. The
# delay grows exponentially so a long outage cannot turn the worker into a
# tight database/network loop, while the one-hour cap keeps recovery bounded.
_DELETE_RETRY_BASE_SECONDS = 30
_DELETE_RETRY_MAX_SECONDS = 3600
# Account-delete operations still use the historical fixed delay. Keep this
# name for that separate state machine and for callers importing the constant.
_DELETE_RETRY_DELAY_SECONDS = _DELETE_RETRY_BASE_SECONDS


def _delete_retry_delay_seconds(attempt_count: int) -> int:
    """Return the durable retry delay for a failed mail-delete attempt."""

    exponent = min(max(int(attempt_count) - 1, 0), 7)
    return min(_DELETE_RETRY_MAX_SECONDS, _DELETE_RETRY_BASE_SECONDS * (1 << exponent))


# ``attempt_count`` is incremented when a delete is claimed.  Computing the
# due time from that counter and ``updated_at`` keeps the retry schedule
# durable without another migration column.
_DELETE_RETRY_DUE_SQL = (
    f"(updated_at + MIN({_DELETE_RETRY_MAX_SECONDS}, "
    f"{_DELETE_RETRY_BASE_SECONDS} * (1 << MIN(MAX(attempt_count - 1, 0), 7)))) <= ?"
)
_VERIFY_RETRY_BASE_SECONDS = 30
_VERIFY_RETRY_MAX_SECONDS = 1800
_INBOX_CURSOR_STRUCT = struct.Struct(">qQ")
_THREAD_PREFIX_RE = re.compile(r"^(?:(?:re|fw|fwd|回复|转发)[:：]\s*)+", re.IGNORECASE)


def _normalize_thread_subject(subject: str | None) -> str:
    """Canonicalize reply/forward prefixes before subject matching.

    Mail clients vary between ASCII and full-width colons and may stack several
    prefixes (``Re: Fwd:``).  Empty subjects intentionally remain empty so a
    missing subject can never become a shared thread key.
    """

    value = " ".join(str(subject or "").strip().split())
    value = _THREAD_PREFIX_RE.sub("", value).strip()
    return " ".join(value.casefold().split()) if value else ""


def _normalize_important_links(value: Any) -> list[dict[str, str]]:
    """Return the bounded, JSON-safe representation used by the mail store.

    LLM output is normally sanitized before it reaches the repository, but the
    repository is also an externally callable persistence boundary.  Accept the
    canonical ``[{caption, link}]`` shape plus the historical ``url``/string
    variants, discard non-http(s) values, remove duplicates, and keep the field
    deterministic for idempotent retries.
    """

    if value is _UNSET or value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            # A bare URL is useful for compatibility with very early adapters.
            value = [raw]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            link = str(item.get("link") or item.get("url") or "").strip()
            caption = str(item.get("caption") or item.get("title") or "").strip()
        elif isinstance(item, str):
            link = item.strip()
            caption = ""
        else:
            continue
        if not link.startswith(("http://", "https://")):
            continue
        try:
            parsed = urlsplit(link)
        except ValueError:
            continue
        # Never persist credentials embedded in a URL.  They could otherwise
        # reach a Telegram inline keyboard or Mini App link on a later read.
        if parsed.username or parsed.password or not parsed.netloc:
            continue
        if link in seen:
            continue
        seen.add(link)
        result.append({"caption": (caption or link)[:128], "link": link[:4096]})
        if len(result) >= 5:
            break
    return result


def _encode_important_links(value: Any) -> str:
    return json.dumps(_normalize_important_links(value), ensure_ascii=False, separators=(",", ":"))


def _decode_important_links(value: Any) -> list[dict[str, str]]:
    """Decode a persisted links field; malformed/legacy values become ``[]``."""

    return _normalize_important_links(value)


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result = dict(row)
    if "llm_important_links_json" in result:
        result["important_links"] = _decode_important_links(result.get("llm_important_links_json"))
    return result


def _secret_aad(account_id: int) -> bytes:
    """Bind encrypted credentials to this account and schema generation."""
    return f"telegramail:v{CURRENT_SCHEMA_VERSION}:account:{int(account_id)}".encode("ascii")


def encode_inbox_cursor(latest_at: int, thread_id: int) -> str:
    """Encode the keyset boundary into compact Telegram-safe callback data."""

    payload = _INBOX_CURSOR_STRUCT.pack(int(latest_at), int(thread_id))
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_inbox_cursor(value: str | None) -> tuple[int, int] | None:
    """Decode a cursor, returning ``None`` for the latest page/invalid input."""

    token = str(value or "").strip()
    if not token or token in {"0", "latest", "none"}:
        return None
    # Callback data is user-controlled.  Keep decoding strict and bounded.
    if len(token) > 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("invalid inbox cursor")
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(raw) != _INBOX_CURSOR_STRUCT.size:
            raise ValueError
        latest_at, thread_id = _INBOX_CURSOR_STRUCT.unpack(raw)
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise ValueError("invalid inbox cursor") from exc
    if thread_id <= 0:
        raise ValueError("invalid inbox cursor")
    return int(latest_at), int(thread_id)


def _fts_query(value: str | None) -> str | None:
    """Turn user search text into a safe, prefix-oriented FTS5 expression."""

    tokens = re.findall(r"[\w@.+-]+", str(value or ""), flags=re.UNICODE)
    if not tokens:
        return None
    # Quote each token so punctuation cannot inject FTS operators.  Prefix
    # matching keeps ``/inbox ali`` useful for sender/subject lookups.
    return " OR ".join(f'"{token.replace(chr(34), "")}"*' for token in tokens[:8])


def _llm_settings_aad() -> bytes:
    """Bind the global LLM credential to this installation/schema."""
    return f"telegramail:v{CURRENT_SCHEMA_VERSION}:llm-settings".encode("ascii")


class Database:
    """Connection factory with short-lived, thread-confined SQLite connections."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._initialize_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._initialize_lock:
            conn = self.connect()
            try:
                initialize_schema(conn)
            finally:
                conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class V2Repository:
    """CRUD and durable operation state machines for SQLite schema v2."""

    _OPERATION_CONFIG = {
        "send": ("send_operations", "sending", "sent"),
        "delete": ("delete_operations", "deleting", "deleted"),
    }

    def __init__(self, path: str | Path, *, master_key: Optional[bytes] = None):
        self.db = Database(path)
        self.db.initialize()
        self._master_key = master_key
        self._projection_checked = False

    def _key(self) -> bytes:
        return self._master_key if self._master_key is not None else load_master_key()

    # Global LLM settings and durable summary queue -----------------------
    def get_llm_settings(self) -> Dict[str, Any]:
        """Return the singleton LLM configuration without decrypting its key.

        The returned mapping is deliberately a public/safe projection.  In
        particular, nonce/ciphertext are not returned to API callers and the
        plaintext API key is never needed for reads.
        """
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM llm_settings WHERE singleton = 1").fetchone()
            if row is None:
                return {
                    "enabled": False,
                    "base_url": "",
                    "model": "",
                    "default_language": "en_US",
                    "summary_threshold": 120,
                    "api_key_configured": False,
                    "last_test_status": "never",
                    "last_tested_at": None,
                    "failed_count": 0,
                    "updated_at": None,
                }
            return {
                "enabled": bool(row["enabled"]),
                "base_url": row["base_url"] or "",
                "model": row["model"] or "",
                "default_language": row["default_language"] or "en_US",
                "summary_threshold": int(row["summary_threshold"] if row["summary_threshold"] is not None else 120),
                "api_key_configured": bool(row["api_key_nonce"] and row["api_key_ciphertext"]),
                "last_test_status": row["last_test_status"] or "never",
                "last_tested_at": row["last_tested_at"],
                "failed_count": int(row["failed_count"] or 0),
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()

    def get_llm_settings_secret(self) -> Optional[str]:
        """Decrypt and return the configured global LLM API key for workers.

        This method is intentionally separate from :meth:`get_llm_settings`
        so a normal HTTP read can never accidentally serialize the secret.
        """
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT api_key_nonce, api_key_ciphertext FROM llm_settings WHERE singleton = 1"
            ).fetchone()
            if row is None or not row["api_key_nonce"] or not row["api_key_ciphertext"]:
                return None
            return decrypt_secret(
                row["api_key_nonce"], row["api_key_ciphertext"], self._key(), aad=_llm_settings_aad()
            ).decode("utf-8")
        finally:
            conn.close()

    # Compatibility aliases used by worker/API adapters.
    get_llm_api_key = get_llm_settings_secret

    def get_global_llm_settings(self) -> Dict[str, Any]:
        """Return worker-facing settings, including the decrypted key.

        This is an internal worker hook; HTTP handlers use
        :meth:`get_llm_settings`, which intentionally omits ``api_key``.
        ``models``/``threshold`` aliases keep the existing summary worker
        duck-typed contract stable while the persisted schema uses singular
        fields.
        """
        settings = self.get_llm_settings()
        api_key = self.get_llm_settings_secret()
        settings["api_key"] = api_key
        settings["models"] = [settings["model"]] if settings.get("model") else []
        settings["threshold"] = settings.get("summary_threshold", 0)
        settings["llm_enabled"] = settings.get("enabled", False)
        settings["llm_base_url"] = settings.get("base_url", "")
        settings["llm_model"] = settings.get("model", "")
        settings["llm_api_key"] = api_key
        return settings

    load_llm_settings = get_global_llm_settings

    def update_llm_settings(self, updates: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        """Create/update the singleton LLM configuration.

        ``api_key`` is write-only: an empty or omitted value leaves the
        existing encrypted key untouched.  The returned value is the same safe
        projection as :meth:`get_llm_settings`.
        """
        values: Dict[str, Any] = dict(updates or {})
        values.update(kwargs)
        allowed = {"enabled", "base_url", "model", "default_language", "summary_threshold", "api_key"}
        values = {key: value for key, value in values.items() if key in allowed}
        if "base_url" in values:
            values["base_url"] = str(values["base_url"] or "").strip()
        if "model" in values:
            values["model"] = str(values["model"] or "").strip()
        if "default_language" in values:
            values["default_language"] = str(values["default_language"] or "en_US").strip() or "en_US"
        if "enabled" in values:
            values["enabled"] = int(bool(values["enabled"]))
        if "summary_threshold" in values:
            try:
                values["summary_threshold"] = max(0, int(values["summary_threshold"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("summary_threshold must be an integer") from exc
        api_key = values.pop("api_key", None)
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute("SELECT * FROM llm_settings WHERE singleton = 1").fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO llm_settings(singleton, enabled, base_url, model,
                             default_language, summary_threshold, created_at, updated_at)
                       VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        values.get("enabled", 0), values.get("base_url", ""), values.get("model", ""),
                        values.get("default_language", "en_US"), values.get("summary_threshold", 120), now, now,
                    ),
                )
            elif values:
                assignments = ", ".join(f"{name} = ?" for name in values)
                conn.execute(
                    f"UPDATE llm_settings SET {assignments}, updated_at = ? WHERE singleton = 1",
                    tuple(values.values()) + (now,),
                )
            # Empty strings intentionally preserve the existing key.  This is
            # useful for PUT forms which submit an untouched password input.
            if api_key is not None and str(api_key).strip():
                nonce, ciphertext = encrypt_secret(str(api_key), self._key(), aad=_llm_settings_aad())
                conn.execute(
                    """UPDATE llm_settings SET api_key_nonce = ?, api_key_ciphertext = ?,
                       updated_at = ? WHERE singleton = 1""",
                    (nonce, ciphertext, now),
                )
            elif existing is None:
                # Keep first-write behavior explicit for a no-key settings row.
                conn.execute("UPDATE llm_settings SET updated_at = ? WHERE singleton = 1", (now,))
        return self.get_llm_settings()

    # Common naming variants make this repository safe to inject into older
    # duck-typed API/worker adapters during the migration window.
    set_llm_settings = update_llm_settings
    save_llm_settings = update_llm_settings

    def record_llm_test(self, status: str, *, tested_at: Optional[int] = None) -> Dict[str, Any]:
        """Persist the latest connection-test result and failure counter."""
        normalized = str(status or "failed").strip().lower()
        if normalized not in {"never", "ok", "success", "failed"}:
            normalized = "failed"
        if normalized == "success":
            normalized = "ok"
        now = int(tested_at) if tested_at is not None else _now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO llm_settings(singleton, last_test_status, last_tested_at,
                           failed_count, created_at, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     last_test_status = excluded.last_test_status,
                     last_tested_at = excluded.last_tested_at,
                     failed_count = CASE WHEN excluded.last_test_status = 'ok'
                                        THEN 0 ELSE llm_settings.failed_count + 1 END,
                     updated_at = excluded.updated_at""",
                (normalized, now, 0 if normalized == "ok" else 1, now, now),
            )
        return self.get_llm_settings()

    # Summary queue -------------------------------------------------------
    def get_summary_task(self, email_id: int) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return _row(conn.execute("SELECT * FROM summary_tasks WHERE email_id = ?", (int(email_id),)).fetchone())
        finally:
            conn.close()

    def enqueue_summary_task(self, email_id: int, *, force: bool = False) -> Dict[str, Any]:
        """Insert or requeue one durable summary task for an email."""
        email_key = int(email_id)
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM emails WHERE id = ?", (email_key,)).fetchone() is None:
                raise KeyError("email does not exist")
            existing = conn.execute("SELECT * FROM summary_tasks WHERE email_id = ?", (email_key,)).fetchone()
            requeued = existing is None
            if existing is None:
                cur = conn.execute(
                    """INSERT INTO summary_tasks(email_id, status, attempts, created_at, updated_at)
                       VALUES (?, 'queued', 0, ?, ?)""",
                    (email_key, now, now),
                )
            elif force or existing["status"] in {"failed", "skipped", "completed", "succeeded"}:
                requeued = True
                conn.execute(
                    """UPDATE summary_tasks SET status = 'queued', lease_token = NULL,
                       lease_until = NULL, last_error = NULL, completed_at = NULL,
                       updated_at = ? WHERE email_id = ?""",
                    (now, email_key),
                )
                cur = None
            else:
                cur = None
            if requeued:
                conn.execute(
                    """UPDATE emails SET summary_status = 'queued', summary_updated_at = ?,
                       updated_at = ? WHERE id = ?""",
                    (now, now, email_key),
                )
            row = conn.execute("SELECT * FROM summary_tasks WHERE email_id = ?", (email_key,)).fetchone()
            return _row(row) or {}

    # Explicit aliases for worker/frontend naming conventions.
    retry_email_summary = enqueue_summary_task
    create_summary_task = enqueue_summary_task

    def claim_summary_task(self, *, lease_seconds: int = 300, include_failed: bool = True) -> Optional[Dict[str, Any]]:
        """Atomically claim the oldest queued/retryable summary task.

        Failed tasks are included for the background retry loop.  Queued work
        always wins over a retry so a single broken provider cannot starve new
        mail.  ``include_failed=False`` is used by diagnostics that only want
        first attempts.
        """
        now = _now()
        token = uuid.uuid4().hex
        with self.db.transaction(immediate=True) as conn:
            statuses = ("queued", "pending", "failed") if include_failed else ("queued", "pending")
            marks = ", ".join("?" for _ in statuses)
            row = conn.execute(
                f"""SELECT * FROM summary_tasks
                   WHERE status IN ({marks})
                      OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?)
                   ORDER BY CASE WHEN status = 'queued' THEN 0
                                      WHEN status = 'pending' THEN 1
                                      WHEN status = 'running' THEN 2
                                      ELSE 3 END,
                            updated_at, id LIMIT 1""",
                (*statuses, now),
            ).fetchone()
            if row is None:
                return None
            task_id = int(row["id"])
            lease_until = now + max(1, int(lease_seconds))
            conn.execute(
                """UPDATE summary_tasks SET status = 'running', attempts = attempts + 1,
                   lease_token = ?, lease_until = ?, updated_at = ? WHERE id = ?""",
                (token, lease_until, now, task_id),
            )
            conn.execute(
                "UPDATE emails SET summary_status = 'running', summary_updated_at = ?, updated_at = ? WHERE id = ?",
                (now, now, int(row["email_id"])),
            )
            result = _row(conn.execute("SELECT * FROM summary_tasks WHERE id = ?", (task_id,)).fetchone()) or {}
            result["lease_token"] = token
            return result

    def complete_summary_task(
        self,
        email_id: int,
        *,
        success: bool = True,
        error: Optional[str] = None,
        status: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Finish a summary task and mirror its status onto ``emails``."""
        email_key = int(email_id)
        now = _now()
        target = status or ("completed" if success else "failed")
        if target not in {"completed", "succeeded", "failed", "skipped", "queued"}:
            target = "completed" if success else "failed"
        with self.db.transaction(immediate=True) as conn:
            where = "email_id = ?"
            params: list[Any] = [email_key]
            if lease_token:
                where += " AND lease_token = ?"
                params.append(str(lease_token))
            row = conn.execute(f"SELECT * FROM summary_tasks WHERE {where}", tuple(params)).fetchone()
            # Callers commonly carry either the task id or the email id.  Both
            # identify the same durable work item, so accept either form while
            # retaining the lease-token guard.
            if row is None:
                where = "id = ?"
                params = [email_key]
                if lease_token:
                    where += " AND lease_token = ?"
                    params.append(str(lease_token))
                row = conn.execute(f"SELECT * FROM summary_tasks WHERE {where}", tuple(params)).fetchone()
            if row is None:
                return None
            email_key = int(row["email_id"])
            conn.execute(
                """UPDATE summary_tasks SET status = ?, lease_token = NULL, lease_until = NULL,
                   last_error = ?, completed_at = CASE WHEN ? IN ('completed', 'succeeded', 'skipped') THEN ? ELSE NULL END,
                   updated_at = ? WHERE id = ?""",
                (target, str(error)[:1000] if error else None, target, now, now, int(row["id"])),
            )
            conn.execute(
                "UPDATE emails SET summary_status = ?, summary_updated_at = ?, updated_at = ? WHERE id = ?",
                (target, now, now, email_key),
            )
            return _row(conn.execute("SELECT * FROM summary_tasks WHERE id = ?", (int(row["id"]),)).fetchone())

    def list_summary_tasks(self, *, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            bounded = min(max(int(limit), 1), 500)
            if status:
                rows = conn.execute(
                    "SELECT * FROM summary_tasks WHERE status = ? ORDER BY updated_at, id LIMIT ?",
                    (str(status), bounded),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM summary_tasks ORDER BY updated_at, id LIMIT ?", (bounded,)).fetchall()
            return [_row(row) or {} for row in rows]
        finally:
            conn.close()

    # Admin binding
    def bind_admin(self, telegram_user_id: int, *, private_chat_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute("SELECT * FROM admin_binding WHERE singleton = 1").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO admin_binding(singleton, telegram_user_id, private_chat_id, created_at, updated_at) VALUES (1, ?, ?, ?, ?)",
                    (int(telegram_user_id), int(private_chat_id) if private_chat_id is not None else None, now, now),
                )
            elif int(existing["telegram_user_id"]) != int(telegram_user_id):
                raise PermissionError("an administrator is already bound")
            else:
                conn.execute("UPDATE admin_binding SET private_chat_id = COALESCE(?, private_chat_id), updated_at = ? WHERE singleton = 1",
                             (int(private_chat_id) if private_chat_id is not None else None, now))
            return _row(conn.execute("SELECT * FROM admin_binding WHERE singleton = 1").fetchone()) or {}

    def set_admin_private_chat(self, telegram_user_id: int, private_chat_id: int) -> Dict[str, Any]:
        """Persist the private-topic projection destination for the bound admin."""
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("UPDATE admin_binding SET private_chat_id = ?, updated_at = ? WHERE singleton = 1 AND telegram_user_id = ?",
                               (int(private_chat_id), now, int(telegram_user_id)))
            if cur.rowcount != 1:
                raise PermissionError("administrator is not bound")
            return _row(conn.execute("SELECT * FROM admin_binding WHERE singleton = 1").fetchone()) or {}

    def get_admin_binding(self) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return _row(conn.execute("SELECT * FROM admin_binding WHERE singleton = 1").fetchone())
        finally:
            conn.close()

    def set_inbox_panel_message(
        self, telegram_user_id: int, chat_id: int, message_id: int,
        *, cursor: str | None = None, search: str | None = None,
    ) -> Dict[str, Any]:
        """Persist the one reusable Telegram inbox panel for the bound admin."""

        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                """UPDATE admin_binding
                   SET inbox_panel_chat_id = ?, inbox_panel_message_id = ?,
                       inbox_panel_cursor = ?, inbox_panel_search = ?, updated_at = ?
                   WHERE singleton = 1 AND telegram_user_id = ?""",
                (int(chat_id), int(message_id), cursor, search, now, int(telegram_user_id)),
            )
            if cur.rowcount != 1:
                raise PermissionError("administrator is not bound")
            return _row(conn.execute("SELECT * FROM admin_binding WHERE singleton = 1").fetchone()) or {}

    def set_inbox_panel_state(
        self, telegram_user_id: int, *, cursor: str | None = None, search: str | None = None,
    ) -> Dict[str, Any]:
        """Persist the current Telegram Inbox location without changing its message."""

        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                """UPDATE admin_binding
                   SET inbox_panel_cursor = ?, inbox_panel_search = ?, updated_at = ?
                   WHERE singleton = 1 AND telegram_user_id = ?""",
                (cursor, search, now, int(telegram_user_id)),
            )
            if cur.rowcount != 1:
                raise PermissionError("administrator is not bound")
            return _row(conn.execute("SELECT * FROM admin_binding WHERE singleton = 1").fetchone()) or {}

    def clear_inbox_panel_message(self, telegram_user_id: int) -> Dict[str, Any]:
        """Clear the retired aggregate Inbox panel pointer and navigation state."""

        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                """UPDATE admin_binding
                   SET inbox_panel_chat_id = NULL, inbox_panel_message_id = NULL,
                       inbox_panel_cursor = NULL, inbox_panel_search = NULL,
                       updated_at = ?
                   WHERE singleton = 1 AND telegram_user_id = ?""",
                (now, int(telegram_user_id)),
            )
            if cur.rowcount != 1:
                raise PermissionError("administrator is not bound")
            return _row(conn.execute("SELECT * FROM admin_binding WHERE singleton = 1").fetchone()) or {}

    def list_telegram_inbox_threads(
        self, *, limit: int = 5, cursor: str | None = None, search: str | None = None,
        filters: Optional[Dict[str, Any]] = None, offset: int | None = None,
    ) -> Dict[str, Any]:
        """Return a stable keyset page of Inbox threads.

        ``offset`` remains accepted for old integrations, but is translated to
        repeated keyset pages instead of issuing a deep ``OFFSET`` query.
        """

        bounded_limit = min(max(int(limit), 1), 10)
        if offset is not None and int(offset) > 0 and not cursor:
            remaining = int(offset)
            if remaining > 10_000:
                raise ValueError("offset compatibility limit exceeded; use a cursor")
            boundary: str | None = None
            while remaining > 0:
                step = min(remaining, bounded_limit)
                page = self.list_telegram_inbox_threads(limit=step, cursor=boundary, search=search, filters=filters)
                rows = page.get("items") or []
                if not rows:
                    return {"items": [], "next_cursor": None, "has_more": False}
                # The compatibility offset is translated into keyset walks. If
                # the final fetched page has no next boundary, the requested
                # offset points at the end (or beyond it), so return an empty
                # page instead of accidentally querying the latest page again.
                if not page.get("has_more"):
                    return {"items": [], "next_cursor": None, "has_more": False}
                boundary = page.get("next_cursor")
                remaining -= len(rows)
            cursor = boundary
        boundary = decode_inbox_cursor(cursor)
        where = [
            "t.status = 'active'",
            "e.tombstoned_at IS NULL",
            "NOT EXISTS (SELECT 1 FROM delete_operations d WHERE d.thread_id = t.id AND d.status IN ('queued', 'deleting', 'failed'))",
        ]
        args: list[Any] = []
        if boundary is not None:
            where.append("(t.latest_at < ? OR (t.latest_at = ? AND t.id < ?))")
            args.extend([boundary[0], boundary[0], boundary[1]])
        fts_query = _fts_query(search)
        fts_arg_index: int | None = None
        if fts_query:
            # Search every message in a thread while still projecting only the
            # materialized latest email.  EXISTS avoids duplicate thread rows
            # when several historical messages match.
            where.append(
                "EXISTS (SELECT 1 FROM emails_fts fts "
                "JOIN emails e_search ON CAST(fts.email_id AS INTEGER) = e_search.id "
                "WHERE fts.thread_id = t.id AND e_search.tombstoned_at IS NULL AND fts MATCH ?)"
            )
            args.append(fts_query)
            fts_arg_index = len(args) - 1
        # ``filters`` is deliberately narrow: Telegram uses this only for
        # future category/priority filters and unknown keys are ignored.
        for key in ("category", "priority"):
            value = (filters or {}).get(key)
            if value:
                where.append(f"e.llm_{key} = ?")
                args.append(str(value))
        conn = self.db.connect()
        try:
            self._ensure_latest_email_projection_once(conn)
            rows = conn.execute(
                f"""SELECT t.id AS thread_id, t.account_id, t.telegram_chat_id, t.telegram_message_thread_id,
                            t.latest_at,
                            (SELECT p.status FROM telegram_delivery_parts p
                             WHERE p.email_id = e.id ORDER BY p.part_index, p.id LIMIT 1)
                                AS telegram_delivery_status,
                            e.id AS latest_email_id, e.sender, e.subject, e.email_date,
                            e.llm_summary, e.llm_important_links_json, e.body_text, e.llm_priority, e.llm_category,
                            e.summary_status, e.summary_updated_at, e.created_at
                     FROM mail_threads t
                     JOIN emails e ON e.id = t.latest_email_id
                     WHERE {' AND '.join(where)}
                     ORDER BY t.latest_at DESC, t.id DESC
                     LIMIT ?""",
                (*args, bounded_limit + 1),
            ).fetchall()
            items = [_row(row) or {} for row in rows[:bounded_limit]]
            has_more = len(rows) > bounded_limit
            next_cursor = None
            if has_more and items:
                last = items[-1]
                next_cursor = encode_inbox_cursor(int(last.get("latest_at") or last.get("created_at") or 0), int(last["thread_id"]))
            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except sqlite3.OperationalError:
            # SQLite builds without FTS5 still provide a useful search fallback.
            if not fts_query:
                raise
            like = f"%{str(search or '').strip()}%"
            fallback_where = [item for item in where if "emails_fts fts" not in item]
            fallback_args = [item for index, item in enumerate(args) if index != fts_arg_index]
            rows = conn.execute(
                f"""SELECT t.id AS thread_id, t.account_id, t.telegram_chat_id, t.telegram_message_thread_id,
                            t.latest_at, NULL AS telegram_delivery_status, e.id AS latest_email_id,
                            e.sender, e.subject, e.email_date, e.llm_summary, e.llm_important_links_json, e.body_text,
                            e.llm_priority, e.llm_category, e.summary_status, e.summary_updated_at, e.created_at
                     FROM mail_threads t JOIN emails e ON e.id = t.latest_email_id
                     WHERE {' AND '.join(fallback_where)}
                       AND EXISTS (
                         SELECT 1 FROM emails e_search
                         WHERE e_search.thread_id = t.id
                           AND e_search.tombstoned_at IS NULL
                           AND (e_search.sender LIKE ? OR e_search.subject LIKE ?
                                OR e_search.llm_summary LIKE ? OR e_search.body_text LIKE ?)
                       )
                     ORDER BY t.latest_at DESC, t.id DESC LIMIT ?""",
                (*fallback_args, like, like, like, like, bounded_limit + 1),
            ).fetchall()
            items = [_row(row) or {} for row in rows[:bounded_limit]]
            has_more = len(rows) > bounded_limit
            next_cursor = encode_inbox_cursor(int(items[-1].get("latest_at") or 0), int(items[-1]["thread_id"])) if has_more and items else None
            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        finally:
            conn.close()

    def get_previous_telegram_inbox_cursor(self, cursor: str | None, *, search: str | None = None) -> str | None:
        """Return the prior page boundary for an empty page after deletion."""

        if not cursor:
            return None
        target = str(cursor)
        boundary: str | None = None
        # Deletion is rare and page size is five; bounded traversal keeps this
        # recovery path simple while normal browsing remains O(page size).
        for _ in range(2000):
            page = self.list_telegram_inbox_threads(limit=5, cursor=boundary, search=search)
            if page.get("next_cursor") == target or not page.get("has_more"):
                return boundary
            boundary = page.get("next_cursor")
        return None

    @staticmethod
    def _ensure_latest_email_projection(conn: sqlite3.Connection) -> None:
        """Repair stale/null latest pointers before using the projection."""

        conn.execute(
            """UPDATE mail_threads
               SET latest_email_id = (
                     SELECT e.id FROM emails e
                     WHERE e.thread_id = mail_threads.id AND e.tombstoned_at IS NULL
                     ORDER BY e.created_at DESC, e.id DESC LIMIT 1
               ),
                   latest_at = (
                     SELECT e.created_at FROM emails e
                     WHERE e.thread_id = mail_threads.id AND e.tombstoned_at IS NULL
                     ORDER BY e.created_at DESC, e.id DESC LIMIT 1
                   )
               WHERE latest_email_id IS NULL
                  OR NOT EXISTS (
                       SELECT 1 FROM emails current_email
                       WHERE current_email.id = mail_threads.latest_email_id
                         AND current_email.thread_id = mail_threads.id
                         AND current_email.tombstoned_at IS NULL
                  )
                  OR latest_at IS NOT (
                       SELECT e.created_at FROM emails e
                       WHERE e.id = mail_threads.latest_email_id
                  )
                  OR EXISTS (
                       SELECT 1
                       FROM emails newer
                       JOIN emails current_email ON current_email.id = mail_threads.latest_email_id
                       WHERE newer.thread_id = mail_threads.id
                         AND newer.tombstoned_at IS NULL
                         AND (newer.created_at > current_email.created_at OR
                              (newer.created_at = current_email.created_at AND newer.id > current_email.id))
                  )"""
        )

    def _ensure_latest_email_projection_once(self, conn: sqlite3.Connection) -> None:
        if self._projection_checked:
            return
        self._ensure_latest_email_projection(conn)
        self._projection_checked = True

    def list_threads(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the Mini App's bounded recent-thread projection."""

        bounded = min(max(int(limit), 1), 50)
        conn = self.db.connect()
        try:
            self._ensure_latest_email_projection_once(conn)
            rows = conn.execute(
                """SELECT t.id, t.account_id, e.email_date AS latest_at, e.subject,
                          e.llm_summary AS summary, e.llm_important_links_json AS llm_important_links_json,
                          e.summary_status AS summary_status,
                          e.llm_priority AS priority, e.llm_category AS category,
                          e.summary_updated_at, COUNT(all_e.id) AS message_count
                   FROM mail_threads t
                   JOIN emails e ON e.id = t.latest_email_id AND e.tombstoned_at IS NULL
                   LEFT JOIN emails all_e ON all_e.thread_id = t.id AND all_e.tombstoned_at IS NULL
                   WHERE t.status = 'active'
                     AND NOT EXISTS (SELECT 1 FROM delete_operations d WHERE d.thread_id = t.id AND d.status IN ('queued', 'deleting', 'failed'))
                   GROUP BY t.id, t.account_id, t.latest_at, e.id
                   ORDER BY t.latest_at DESC, t.id DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
            return [_row(row) or {} for row in rows]
        finally:
            conn.close()

    def get_telegram_inbox_thread(self, thread_id: int) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            self._ensure_latest_email_projection_once(conn)
            row = conn.execute(
                """SELECT t.id AS thread_id, t.account_id, t.telegram_chat_id,
                          t.telegram_message_thread_id, t.status,
                          (SELECT p.status FROM telegram_delivery_parts p
                           WHERE p.email_id = e.id ORDER BY p.part_index, p.id LIMIT 1)
                              AS telegram_delivery_status,
                          e.sender, e.subject,
                          e.email_date, e.llm_summary, e.llm_important_links_json, e.body_text, e.llm_priority,
                          e.llm_category, e.summary_status, e.summary_updated_at
                   FROM mail_threads t
                   JOIN emails e ON e.id = t.latest_email_id AND e.tombstoned_at IS NULL
                   WHERE t.id = ? AND t.status <> 'tombstoned'""",
                (int(thread_id),),
            ).fetchone()
            return _row(row)
        finally:
            conn.close()

    def list_telegram_delivery_parts(self, email_id: int, *, delivered_only: bool = True) -> List[Dict[str, Any]]:
        """Return Telegram message mappings for one email in display order.

        Summary completion uses these durable message ids to edit an existing
        Topic card in place.  No new Topic or message is created by this read.
        """
        conn = self.db.connect()
        try:
            where = "email_id = ? AND status = 'delivered'" if delivered_only else "email_id = ?"
            rows = conn.execute(
                f"SELECT * FROM telegram_delivery_parts WHERE {where} ORDER BY part_index, id",
                (int(email_id),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def record_telegram_delivery_parts(
        self, email_id: int, chat_id: int, message_ids: Sequence[int], *, start_index: int = 1
    ) -> List[Dict[str, Any]]:
        """Persist additional Telegram message ids after a card is delivered.

        Part ``0`` is owned by the projection lease and is completed by
        ``complete_projection``.  Additional Bot API chunks are recorded here
        only after the complete send succeeds, avoiding a false ``delivered``
        state while a multi-part request is still in flight.
        """
        ids = [int(value) for value in message_ids if value is not None]
        if not ids:
            return []
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            for offset, message_id in enumerate(ids, start=max(1, int(start_index))):
                conn.execute(
                    """INSERT INTO telegram_delivery_parts(
                           email_id, part_index, telegram_chat_id, telegram_message_id,
                           status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'delivered', ?, ?)
                       ON CONFLICT(email_id, part_index) DO UPDATE SET
                         telegram_chat_id = excluded.telegram_chat_id,
                         telegram_message_id = excluded.telegram_message_id,
                         status = 'delivered', lease_token = NULL, lease_until = NULL,
                         updated_at = excluded.updated_at""",
                    (int(email_id), int(offset), int(chat_id), message_id, now, now),
                )
            rows = conn.execute(
                "SELECT * FROM telegram_delivery_parts WHERE email_id = ? ORDER BY part_index, id",
                (int(email_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_topic_delete_markup_targets(self, *, limit: int = 5) -> List[Dict[str, Any]]:
        """Return delivered Topic messages that still need the delete control."""

        conn = self.db.connect()
        try:
            rows = conn.execute(
                """SELECT p.id AS delivery_part_id, p.email_id, p.part_index,
                          p.telegram_message_id, e.llm_important_links_json,
                          t.id AS thread_id, t.telegram_chat_id,
                          t.telegram_message_thread_id
                   FROM telegram_delivery_parts p
                   JOIN emails e ON e.id = p.email_id
                   JOIN mail_threads t ON t.id = e.thread_id
                   WHERE p.status = 'delivered'
                     AND p.part_index = 0
                     AND p.telegram_message_id IS NOT NULL
                     AND (p.topic_delete_markup_version < 1 OR p.topic_action_markup_version < 1)
                     AND e.tombstoned_at IS NULL
                     AND t.status = 'active'
                     AND t.telegram_chat_id IS NOT NULL
                     AND t.telegram_message_thread_id IS NOT NULL
                   ORDER BY p.topic_delete_markup_attempts, p.updated_at, p.id
                   LIMIT ?""",
                (min(max(int(limit), 1), 20),),
            ).fetchall()
            return [_row(row) or {} for row in rows]
        finally:
            conn.close()

    def complete_topic_delete_markup_backfill(self, delivery_part_id: int, *, success: bool) -> Dict[str, Any]:
        """Persist one idempotent historical Topic-control backfill attempt."""

        now = _now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """UPDATE telegram_delivery_parts
                   SET topic_delete_markup_version = CASE WHEN ? THEN 1 ELSE topic_delete_markup_version END,
                       topic_action_markup_version = CASE WHEN ? THEN 1 ELSE topic_action_markup_version END,
                       topic_delete_markup_attempts = topic_delete_markup_attempts + 1,
                       updated_at = ?
                   WHERE id = ?""",
                (int(bool(success)), int(bool(success)), now, int(delivery_part_id)),
            )
            row = conn.execute(
                "SELECT * FROM telegram_delivery_parts WHERE id = ?", (int(delivery_part_id),)
            ).fetchone()
            if row is None:
                raise KeyError("Telegram delivery part does not exist")
            return dict(row)

    # Accounts and encrypted credentials
    def create_account(self, account: Dict[str, Any], password: str | bytes) -> Dict[str, Any]:
        now = _now()
        email = str(account["email"]).strip().lower()
        initial_enabled = int(bool(account.get("enabled", True)))
        initial_status = "connected" if initial_enabled else "checking"
        initial_next_verification = None if initial_enabled else now
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT id, deleted_at FROM accounts WHERE email = ? AND smtp_server = ?",
                (email, str(account["smtp_server"]).strip()),
            ).fetchone()
            if existing is not None and existing["deleted_at"] is not None:
                pending_purge = conn.execute(
                    """SELECT 1 FROM account_delete_operations
                       WHERE account_id = ? AND purge_data = 1
                         AND status IN ('queued', 'deleting', 'failed')
                       LIMIT 1""",
                    (int(existing["id"]),),
                ).fetchone()
                if pending_purge is not None:
                    # A purge operation is bound to this account id. Reusing the
                    # soft-deleted row before it finishes could let a retry delete
                    # the newly-created account and its unrelated data.
                    raise RuntimeError("account cleanup is still in progress")
                account_id = int(existing["id"])
                conn.execute(
                    """UPDATE accounts SET email = ?, alias = ?, imap_server = ?, imap_port = ?, imap_ssl = ?,
                       smtp_server = ?, smtp_port = ?, smtp_ssl = ?, imap_monitored_mailboxes = ?, signature = ?,
                       enabled = 0, deleted_at = NULL, connection_status = 'checking', connection_error = NULL,
                       last_verified_at = NULL, next_verification_at = ?, verification_attempts = 0, updated_at = ?
                       WHERE id = ?""",
                    (email, str(account.get("alias") or "").strip(), str(account["imap_server"]).strip(),
                     int(account["imap_port"]), int(bool(account.get("imap_ssl", True))),
                     str(account["smtp_server"]).strip(), int(account["smtp_port"]),
                     int(bool(account.get("smtp_ssl", True))), account.get("imap_monitored_mailboxes"),
                     account.get("signature"), now, now, account_id),
                )
                nonce, ciphertext = encrypt_secret(password, self._key(), aad=_secret_aad(account_id))
                conn.execute(
                    """INSERT INTO account_secrets(account_id, nonce, ciphertext, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(account_id) DO UPDATE SET nonce=excluded.nonce,
                       ciphertext=excluded.ciphertext, updated_at=excluded.updated_at""",
                    (account_id, nonce, ciphertext, now, now),
                )
                return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()) or {}
            cursor = conn.execute(
                """INSERT INTO accounts(email, alias, imap_server, imap_port, imap_ssl, smtp_server, smtp_port, smtp_ssl,
                                        imap_monitored_mailboxes, signature, enabled, connection_status,
                                        next_verification_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (email, str(account.get("alias") or "").strip(), str(account["imap_server"]).strip(), int(account["imap_port"]),
                 int(bool(account.get("imap_ssl", True))), str(account["smtp_server"]).strip(), int(account["smtp_port"]),
                 int(bool(account.get("smtp_ssl", True))), account.get("imap_monitored_mailboxes"), account.get("signature"),
                 initial_enabled, initial_status, initial_next_verification, now, now),
            )
            account_id = int(cursor.lastrowid)
            nonce, ciphertext = encrypt_secret(password, self._key(), aad=_secret_aad(account_id))
            conn.execute("INSERT INTO account_secrets(account_id, nonce, ciphertext, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                         (account_id, nonce, ciphertext, now, now))
            return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()) or {}

    def get_account(self, account_id: int, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            query = "SELECT * FROM accounts WHERE id = ?"
            if not include_deleted:
                query += " AND deleted_at IS NULL"
            return _row(conn.execute(query, (int(account_id),)).fetchone())
        finally:
            conn.close()

    def list_accounts(self) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM accounts WHERE deleted_at IS NULL ORDER BY id").fetchall()]
        finally:
            conn.close()

    def update_account(self, account_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {"email", "alias", "imap_server", "imap_port", "imap_ssl", "smtp_server", "smtp_port", "smtp_ssl", "imap_monitored_mailboxes", "signature", "enabled"}
        fields = {key: value for key, value in updates.items() if key in allowed}
        if "email" in fields:
            fields["email"] = str(fields["email"]).strip().lower()
        if not fields:
            return self.get_account(account_id)
        fields["updated_at"] = _now()
        with self.db.transaction(immediate=True) as conn:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ? AND deleted_at IS NULL",
                tuple(fields.values()) + (int(account_id),),
            )
            return _row(conn.execute("SELECT * FROM accounts WHERE id = ? AND deleted_at IS NULL", (int(account_id),)).fetchone())

    def mark_account_verification_pending(self, account_id: int, *, now: Optional[int] = None) -> Optional[Dict[str, Any]]:
        timestamp = _now() if now is None else int(now)
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """UPDATE accounts SET enabled = 0, connection_status = 'checking', connection_error = NULL,
                   next_verification_at = ?, verification_attempts = 0, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (timestamp, timestamp, int(account_id)),
            )
            return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone())

    def claim_accounts_for_verification(self, *, now: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        timestamp = _now() if now is None else int(now)
        bounded = max(1, min(int(limit), 100))
        conn = self.db.connect()
        try:
            rows = conn.execute(
                """SELECT a.* FROM accounts a
                   JOIN account_secrets s ON s.account_id = a.id
                   WHERE a.deleted_at IS NULL AND a.enabled = 0
                     AND (a.next_verification_at IS NULL OR a.next_verification_at <= ?)
                   ORDER BY COALESCE(a.next_verification_at, 0), a.id LIMIT ?""",
                (timestamp, bounded),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def record_account_verification(
        self,
        account_id: int,
        *,
        ok: bool,
        error: Optional[str] = None,
        now: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        timestamp = _now() if now is None else int(now)
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT verification_attempts FROM accounts WHERE id = ? AND deleted_at IS NULL",
                (int(account_id),),
            ).fetchone()
            if current is None:
                return None
            attempts = 0 if ok else int(current["verification_attempts"] or 0) + 1
            if ok:
                next_attempt = None
                status = "connected"
                message = None
                enabled = 1
            else:
                delay = min(_VERIFY_RETRY_MAX_SECONDS, _VERIFY_RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 6)))
                next_attempt = timestamp + delay
                status = "failed"
                message = str(error or "connection")[:200]
                enabled = 0
            conn.execute(
                """UPDATE accounts SET enabled = ?, connection_status = ?, connection_error = ?,
                   last_verified_at = ?, next_verification_at = ?, verification_attempts = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (enabled, status, message, timestamp, next_attempt, attempts, timestamp, int(account_id)),
            )
            return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone())

    def update_account_connection_status(
        self,
        account_id: int,
        *,
        ok: bool,
        error: Optional[str] = None,
        now: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record the latest sync/send result without changing lifecycle state."""
        timestamp = _now() if now is None else int(now)
        status = "connected" if ok else "failed"
        message = None if ok else str(error or "connection")[:200]
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """UPDATE accounts SET connection_status = ?, connection_error = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (status, message, timestamp, int(account_id)),
            )
            return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone())

    def soft_delete_account(self, account_id: int, *, now: Optional[int] = None) -> Optional[Dict[str, Any]]:
        timestamp = _now() if now is None else int(now)
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ? AND deleted_at IS NULL", (int(account_id),)).fetchone()
            if row is None:
                return None
            conn.execute(
                """DELETE FROM account_secrets WHERE account_id = ?;
                   """,
                (int(account_id),),
            )
            conn.execute(
                """UPDATE accounts SET enabled = 0, deleted_at = ?, connection_status = 'deleted',
                   connection_error = NULL, next_verification_at = NULL, updated_at = ? WHERE id = ?""",
                (timestamp, timestamp, int(account_id)),
            )
            return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone())

    def list_account_topics(self, account_id: int) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute(
                """SELECT id, telegram_chat_id, telegram_message_thread_id FROM mail_threads
                   WHERE account_id = ? AND status <> 'tombstoned'
                     AND telegram_chat_id IS NOT NULL AND telegram_message_thread_id IS NOT NULL
                   ORDER BY id""",
                (int(account_id),),
            ).fetchall()]
        finally:
            conn.close()

    def hard_delete_account(self, account_id: int) -> bool:
        with self.db.transaction(immediate=True) as conn:
            attachments = [
                (int(row["draft_id"]), row["local_path"])
                for row in conn.execute(
                    """SELECT da.draft_id, da.local_path FROM draft_attachments da
                       JOIN drafts d ON d.id = da.draft_id
                       WHERE d.account_id = ? AND da.local_path IS NOT NULL""",
                    (int(account_id),),
                ).fetchall()
            ]
            inline_assets = [
                (int(row["email_id"]), row["local_path"])
                for row in conn.execute(
                    """SELECT a.email_id, a.local_path FROM email_inline_assets a
                       JOIN emails e ON e.id = a.email_id
                       WHERE e.account_id = ?""",
                    (int(account_id),),
                ).fetchall()
            ]
            email_attachments = [
                (int(row["email_id"]), row["local_path"])
                for row in conn.execute(
                    """SELECT a.email_id, a.local_path FROM email_attachments a
                       JOIN emails e ON e.id = a.email_id
                       WHERE e.account_id = ?""",
                    (int(account_id),),
                ).fetchall()
            ]
            cursor = conn.execute("DELETE FROM accounts WHERE id = ?", (int(account_id),))
            deleted = cursor.rowcount > 0
        if not deleted:
            return False
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        for draft_id, local_path in attachments:
            attachment_root = (data_root / "attachments" / str(draft_id)).resolve()
            candidate = (data_root / str(local_path)).resolve()
            if candidate.is_relative_to(attachment_root):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                attachment_root.rmdir()
            except OSError:
                pass
        for email_id, local_path in inline_assets:
            asset_root = (data_root / "inline-assets" / str(email_id)).resolve()
            candidate = (data_root / str(local_path)).resolve()
            if candidate.is_relative_to(asset_root):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                asset_root.rmdir()
            except OSError:
                pass
        for email_id, local_path in email_attachments:
            attachment_root = (data_root / "email-attachments" / str(email_id)).resolve()
            candidate = (data_root / str(local_path)).resolve()
            if candidate.is_relative_to(attachment_root):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            _cleanup_email_attachment_root(data_root, email_id)
        return True

    def create_account_delete_operation(self, account_id: int, *, purge_data: bool = True) -> Dict[str, Any]:
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            account = conn.execute("SELECT id, deleted_at FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if account is None:
                raise KeyError("account does not exist")
            existing = conn.execute(
                """SELECT * FROM account_delete_operations WHERE account_id = ?
                   AND status IN ('queued', 'deleting', 'failed') ORDER BY id DESC LIMIT 1""",
                (int(account_id),),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """INSERT INTO account_delete_operations(account_id, status, purge_data, created_at, updated_at)
                   VALUES (?, 'queued', ?, ?, ?)""",
                (int(account_id), int(bool(purge_data)), now, now),
            )
            return dict(conn.execute("SELECT * FROM account_delete_operations WHERE id = last_insert_rowid()").fetchone())

    def claim_next_account_delete(self, *, lease_token: str, lease_seconds: int = 60, now: Optional[int] = None) -> Optional[Dict[str, Any]]:
        timestamp = _now() if now is None else int(now)
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM account_delete_operations
                   WHERE (status = 'queued' OR (status = 'failed' AND updated_at <= ?))
                     AND (lease_until IS NULL OR lease_until <= ?)
                   ORDER BY updated_at, id LIMIT 1""",
                (timestamp - _DELETE_RETRY_DELAY_SECONDS, timestamp),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE account_delete_operations SET status = 'deleting', lease_token = ?,
                   lease_until = ?, attempt_count = attempt_count + 1, updated_at = ? WHERE id = ?""",
                (str(lease_token), timestamp + max(1, int(lease_seconds)), timestamp, int(row["id"])),
            )
            return dict(conn.execute("SELECT * FROM account_delete_operations WHERE id = ?", (int(row["id"]),)).fetchone())

    def update_account_delete_operation(self, operation_id: int, *, status: str, error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        timestamp = _now()
        with self.db.transaction(immediate=True) as conn:
            completed = timestamp if status == "deleted" else None
            conn.execute(
                """UPDATE account_delete_operations SET status = ?, last_error = ?, lease_token = NULL,
                   lease_until = NULL, completed_at = ?, updated_at = ? WHERE id = ?""",
                (status, str(error)[:200] if error else None, completed, timestamp, int(operation_id)),
            )
            return _row(conn.execute("SELECT * FROM account_delete_operations WHERE id = ?", (int(operation_id),)).fetchone())

    def set_account_password(self, account_id: int, password: str | bytes) -> None:
        now = _now()
        nonce, ciphertext = encrypt_secret(password, self._key(), aad=_secret_aad(account_id))
        with self.db.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (int(account_id),)).fetchone() is None:
                raise KeyError("account does not exist")
            conn.execute("""INSERT INTO account_secrets(account_id, nonce, ciphertext, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(account_id) DO UPDATE SET nonce=excluded.nonce, ciphertext=excluded.ciphertext, updated_at=excluded.updated_at""",
                         (int(account_id), nonce, ciphertext, now, now))

    def get_account_password(self, account_id: int) -> str:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT nonce, ciphertext FROM account_secrets WHERE account_id = ?", (int(account_id),)).fetchone()
            if row is None:
                raise KeyError("account secret does not exist")
            return decrypt_secret(row["nonce"], row["ciphertext"], self._key(), aad=_secret_aad(account_id)).decode("utf-8")
        finally:
            conn.close()

    # Identities
    def upsert_identity(self, account_id: int, from_email: str, display_name: str = "", *, reply_to: Optional[str] = None,
                        is_default: bool = False, enabled: bool = True) -> Dict[str, Any]:
        now = _now()
        normalized = from_email.strip().lower()
        with self.db.transaction(immediate=True) as conn:
            if is_default:
                conn.execute("UPDATE account_identities SET is_default = 0, updated_at = ? WHERE account_id = ?", (now, int(account_id)))
            conn.execute("""INSERT INTO account_identities(account_id, from_email, display_name, reply_to, is_default, enabled, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(account_id, from_email) DO UPDATE SET display_name=excluded.display_name, reply_to=excluded.reply_to,
                            is_default=excluded.is_default, enabled=excluded.enabled, updated_at=excluded.updated_at""",
                         (int(account_id), normalized, display_name.strip(), reply_to, int(is_default), int(enabled), now, now))
            return _row(conn.execute("SELECT * FROM account_identities WHERE account_id = ? AND from_email = ?", (int(account_id), normalized)).fetchone()) or {}

    def list_identities(self, account_id: int, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            where = "WHERE account_id = ?" + (" AND enabled = 1" if enabled_only else "")
            return [dict(row) for row in conn.execute(f"SELECT * FROM account_identities {where} ORDER BY is_default DESC, id", (int(account_id),)).fetchall()]
        finally:
            conn.close()

    # Contacts
    def upsert_contact(self, account_id: int, email: str, display_name: str = "", *, seen_at: Optional[int] = None, increment_frequency: bool = True) -> Dict[str, Any]:
        now = _now()
        seen = int(seen_at) if seen_at is not None else now
        normalized = email.strip().lower()
        with self.db.transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO contacts(account_id, email, display_name, frequency, last_seen_at, usage_count, last_used_at, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(account_id, email) DO UPDATE SET
                              display_name=CASE WHEN excluded.display_name <> '' THEN excluded.display_name ELSE contacts.display_name END,
                              frequency=contacts.frequency + ?, last_seen_at=CASE WHEN contacts.last_seen_at IS NULL THEN excluded.last_seen_at ELSE MAX(contacts.last_seen_at, excluded.last_seen_at) END,
                              usage_count=contacts.usage_count + ?, last_used_at=CASE WHEN contacts.last_used_at IS NULL THEN excluded.last_used_at ELSE MAX(contacts.last_used_at, excluded.last_used_at) END, updated_at=excluded.updated_at""",
                         (int(account_id), normalized, display_name.strip(), int(increment_frequency), seen, int(increment_frequency), seen, now, now,
                          int(increment_frequency), int(increment_frequency)))
            return _row(conn.execute("SELECT * FROM contacts WHERE account_id = ? AND email = ?", (int(account_id), normalized)).fetchone()) or {}

    def search_contacts(self, account_id: int, query: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        term = query.strip().lower()
        if not term:
            return []
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute("""
                SELECT *, CASE WHEN lower(email) = ? THEN 0 WHEN lower(email) LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END AS match_rank
                FROM contacts WHERE account_id = ? AND (lower(email) LIKE ? ESCAPE '\\' OR lower(display_name) LIKE ? ESCAPE '\\')
                ORDER BY match_rank, frequency DESC, last_seen_at DESC, email COLLATE NOCASE ASC LIMIT ?
                """, (term, escaped + "%", int(account_id), "%" + escaped + "%", "%" + escaped + "%", max(1, int(limit)))).fetchall()]
        finally:
            conn.close()

    def record_contact_history(self, account_id: int, email: str, display_name: str = "", *, used_at: Optional[int] = None) -> Dict[str, Any]:
        """Migration-safe contact backfill; unknown legacy dates remain unset."""
        normalized = email.strip().lower()
        if not normalized or "@" not in normalized:
            raise ValueError("contact email is not valid")
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO contacts(account_id, email, display_name, frequency, last_seen_at, usage_count, last_used_at, created_at, updated_at)
                            VALUES (?, ?, ?, 1, ?, 1, ?, ?, ?)
                            ON CONFLICT(account_id, email) DO UPDATE SET
                              display_name=CASE WHEN excluded.display_name <> '' THEN excluded.display_name ELSE contacts.display_name END,
                              frequency=contacts.frequency + 1, usage_count=contacts.usage_count + 1,
                              last_seen_at=CASE WHEN excluded.last_seen_at IS NULL THEN contacts.last_seen_at WHEN contacts.last_seen_at IS NULL THEN excluded.last_seen_at ELSE MAX(contacts.last_seen_at, excluded.last_seen_at) END,
                              last_used_at=CASE WHEN excluded.last_used_at IS NULL THEN contacts.last_used_at WHEN contacts.last_used_at IS NULL THEN excluded.last_used_at ELSE MAX(contacts.last_used_at, excluded.last_used_at) END,
                              updated_at=excluded.updated_at""",
                         (int(account_id), normalized, display_name.strip(), used_at, used_at, now, now))
            return _row(conn.execute("SELECT * FROM contacts WHERE account_id = ? AND email = ?", (int(account_id), normalized)).fetchone()) or {}

    @staticmethod
    def _uidvalidity_key(uidvalidity: Optional[str | int]) -> str:
        return "" if uidvalidity is None else str(uidvalidity).strip()

    def get_max_uid(self, account_id: int, mailbox: str = "INBOX", *, uidvalidity: Optional[str | int] = None) -> Optional[int]:
        """Return the greatest numeric IMAP UID for one account/mailbox/epoch."""
        conn = self.db.connect()
        try:
            row = conn.execute("""SELECT MAX(CAST(uid AS INTEGER)) AS maximum FROM emails
                                  WHERE account_id = ? AND mailbox = ? AND uidvalidity = ? AND uid <> '' AND uid NOT GLOB '*[^0-9]*'""",
                               (int(account_id), mailbox.strip() or "INBOX", self._uidvalidity_key(uidvalidity))).fetchone()
            return int(row["maximum"]) if row is not None and row["maximum"] is not None else None
        finally:
            conn.close()

    def get_max_uid_for_mailbox(self, account_id: int, mailbox: str = "INBOX", *, uidvalidity: Optional[str | int] = None) -> Optional[int]:
        return self.get_max_uid(account_id, mailbox, uidvalidity=uidvalidity)

    @staticmethod
    def _cursor_mailbox(mailbox: str) -> str:
        return (mailbox or "").strip() or "INBOX"

    @staticmethod
    def _cursor_last_uid(last_uid: int | str) -> int:
        value = int(last_uid)
        if value < 0:
            raise ValueError("last UID cannot be negative")
        return value

    def get_imap_cursor(self, account_id: int, mailbox: str = "INBOX") -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return _row(conn.execute("SELECT * FROM imap_cursors WHERE account_id = ? AND mailbox = ?",
                                     (int(account_id), self._cursor_mailbox(mailbox))).fetchone())
        finally:
            conn.close()

    def get_or_bootstrap_imap_cursor(self, account_id: int, mailbox: str = "INBOX") -> Dict[str, Any]:
        """Return a durable cursor, bootstrapping ``last_uid`` from legacy rows once."""
        account_key, mailbox_key, now = int(account_id), self._cursor_mailbox(mailbox), _now()
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute("SELECT * FROM imap_cursors WHERE account_id = ? AND mailbox = ?", (account_key, mailbox_key)).fetchone()
            if existing is not None:
                result = dict(existing)
                result["bootstrapped"] = False
                return result
            row = conn.execute("""SELECT MAX(CAST(uid AS INTEGER)) AS maximum FROM emails
                                  WHERE account_id = ? AND mailbox = ? AND uidvalidity = '' AND uid <> '' AND uid NOT GLOB '*[^0-9]*'""",
                               (account_key, mailbox_key)).fetchone()
            last_uid = int(row["maximum"] or 0)
            conn.execute("INSERT INTO imap_cursors(account_id, mailbox, uidvalidity, last_uid, created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
                         (account_key, mailbox_key, last_uid, now, now))
            result = _row(conn.execute("SELECT * FROM imap_cursors WHERE account_id = ? AND mailbox = ?", (account_key, mailbox_key)).fetchone()) or {}
            result["bootstrapped"] = True
            return result

    def reset_imap_cursor(self, account_id: int, mailbox: str, uidvalidity: str | int, *, last_uid: int = 0) -> Dict[str, Any]:
        """Atomically replace the cursor after an IMAP UIDVALIDITY change."""
        account_key, mailbox_key, now = int(account_id), self._cursor_mailbox(mailbox), _now()
        uidvalidity_key = str(uidvalidity).strip()
        if not uidvalidity_key:
            raise ValueError("UIDVALIDITY is required")
        with self.db.transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO imap_cursors(account_id, mailbox, uidvalidity, last_uid, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(account_id, mailbox) DO UPDATE SET uidvalidity=excluded.uidvalidity,
                            last_uid=excluded.last_uid, updated_at=excluded.updated_at""",
                         (account_key, mailbox_key, uidvalidity_key, self._cursor_last_uid(last_uid), now, now))
            result = _row(conn.execute("SELECT * FROM imap_cursors WHERE account_id = ? AND mailbox = ?", (account_key, mailbox_key)).fetchone()) or {}
            result["reset"] = True
            return result

    def advance_imap_cursor(self, account_id: int, mailbox: str, uidvalidity: str | int, last_uid: int) -> Dict[str, Any]:
        """CAS-advance a cursor; callers must reset rather than crossing UIDVALIDITY."""
        account_key, mailbox_key, now = int(account_id), self._cursor_mailbox(mailbox), _now()
        uidvalidity_key = str(uidvalidity).strip()
        if not uidvalidity_key:
            raise ValueError("UIDVALIDITY is required")
        next_uid = self._cursor_last_uid(last_uid)
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute("SELECT * FROM imap_cursors WHERE account_id = ? AND mailbox = ?", (account_key, mailbox_key)).fetchone()
            if current is None:
                conn.execute("INSERT INTO imap_cursors(account_id, mailbox, uidvalidity, last_uid, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (account_key, mailbox_key, uidvalidity_key, next_uid, now, now))
            elif current["uidvalidity"] not in (None, uidvalidity_key):
                result = dict(current)
                result["reset_required"] = True
                return result
            else:
                conn.execute("""UPDATE imap_cursors SET uidvalidity = ?, last_uid = CASE WHEN last_uid > ? THEN last_uid ELSE ? END,
                                updated_at = ? WHERE account_id = ? AND mailbox = ?""",
                             (uidvalidity_key, next_uid, next_uid, now, account_key, mailbox_key))
            result = _row(conn.execute("SELECT * FROM imap_cursors WHERE account_id = ? AND mailbox = ?", (account_key, mailbox_key)).fetchone()) or {}
            result["reset_required"] = False
            return result

    def set_imap_cursor(self, account_id: int, mailbox: str, uidvalidity: str | int, last_uid: int) -> Dict[str, Any]:
        """Compatibility alias for monotonic cursor advancement."""
        return self.advance_imap_cursor(account_id, mailbox, uidvalidity, last_uid)

    def update_email_llm_labels(self, *, email_id: int, category: str, priority: str,
                                confidence: Optional[float] = None, labeled_at: Optional[int] = None,
                                summary: object = _UNSET,
                                important_links: object = _UNSET,
                                important_links_json: object = _UNSET,
                                urls: object = _UNSET) -> Optional[Dict[str, Any]]:
        """Write the legacy-compatible LLM label fields on a v2 email."""
        confidence_value = None if confidence is None else max(0.0, min(1.0, float(confidence)))
        timestamp = int(labeled_at) if labeled_at is not None else _now()
        fields: Dict[str, Any] = {
            "llm_category": (category or "").strip().lower() or "other",
            "llm_priority": (priority or "").strip().lower() or "medium",
            "llm_confidence": confidence_value,
            "llm_labeled_at": timestamp,
            "updated_at": _now(),
        }
        if summary is not _UNSET:
            fields["llm_summary"] = None if summary is None else str(summary)
            fields["summary_status"] = "completed" if summary is not None and str(summary).strip() else "pending"
            fields["summary_updated_at"] = timestamp
        if important_links is _UNSET:
            if important_links_json is not _UNSET:
                important_links = important_links_json
            elif urls is not _UNSET:
                # ``urls`` was the original LLM response key.  Keep it as a
                # write alias while storing one canonical column.
                important_links = urls
        if important_links is not _UNSET:
            fields["llm_important_links_json"] = _encode_important_links(important_links)
        with self.db.transaction(immediate=True) as conn:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            cur = conn.execute(f"UPDATE emails SET {assignments} WHERE id = ?", tuple(fields.values()) + (int(email_id),))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM emails WHERE id = ?", (int(email_id),)).fetchone())

    def get_email(self, email_id: int) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return _row(conn.execute("SELECT * FROM emails WHERE id = ?", (int(email_id),)).fetchone())
        finally:
            conn.close()

    def upsert_email_attachment(
        self,
        email_id: int,
        *,
        part_index: int,
        file_name: str,
        mime_type: str,
        size: int,
        local_path: str,
        sha256: str,
    ) -> Dict[str, Any]:
        """Persist one non-inline incoming MIME part idempotently."""

        if int(part_index) < 0:
            raise ValueError("attachment part_index must be non-negative")
        if int(size) < 0:
            raise ValueError("attachment size must be non-negative")
        if not str(local_path or "").strip():
            raise ValueError("attachment local_path is required")
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO email_attachments(
                         email_id, part_index, file_name, mime_type, size,
                         local_path, sha256, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(email_id, part_index) DO UPDATE SET
                     file_name = excluded.file_name,
                     mime_type = excluded.mime_type,
                     size = excluded.size,
                     local_path = excluded.local_path,
                     sha256 = excluded.sha256,
                     updated_at = excluded.updated_at""",
                (
                    int(email_id), int(part_index), str(file_name or "attachment"),
                    str(mime_type or "application/octet-stream"), int(size),
                    str(local_path), str(sha256), now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM email_attachments WHERE email_id = ? AND part_index = ?",
                (int(email_id), int(part_index)),
            ).fetchone()
            return _row(row) or {}

    def list_email_attachments(self, email_id: int) -> List[Dict[str, Any]]:
        """Return persisted non-inline MIME parts in original order."""

        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM email_attachments WHERE email_id = ? ORDER BY part_index, id",
                (int(email_id),),
            ).fetchall()]
        finally:
            conn.close()

    def upsert_email_inline_asset(
        self,
        email_id: int,
        *,
        content_id: str,
        file_name: str,
        mime_type: str,
        size: int,
        local_path: str,
        sha256: str,
    ) -> Dict[str, Any]:
        """Persist one authenticated-renderable ``cid:`` resource idempotently."""
        normalized_cid = str(content_id or "").strip()
        if not normalized_cid:
            raise ValueError("inline asset content_id is required")
        if int(size) < 0:
            raise ValueError("inline asset size is invalid")
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO email_inline_assets(
                         email_id, content_id, file_name, mime_type, size,
                         local_path, sha256, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(email_id, content_id) DO UPDATE SET
                     file_name = excluded.file_name,
                     mime_type = excluded.mime_type,
                     size = excluded.size,
                     local_path = excluded.local_path,
                     sha256 = excluded.sha256,
                     updated_at = excluded.updated_at""",
                (
                    int(email_id), normalized_cid, str(file_name or "inline-image"),
                    str(mime_type or "application/octet-stream"), int(size),
                    str(local_path), str(sha256), now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM email_inline_assets WHERE email_id = ? AND content_id = ?",
                (int(email_id), normalized_cid),
            ).fetchone()
            return _row(row) or {}

    def list_email_inline_assets(self, email_id: int) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT id, email_id, content_id, file_name, mime_type, size FROM email_inline_assets WHERE email_id = ? ORDER BY id",
                (int(email_id),),
            ).fetchall()]
        finally:
            conn.close()

    def get_email_inline_asset(self, email_id: int, asset_id: int) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return _row(conn.execute(
                """SELECT a.* FROM email_inline_assets a
                   JOIN emails e ON e.id = a.email_id
                   WHERE a.email_id = ? AND a.id = ? AND e.tombstoned_at IS NULL""",
                (int(email_id), int(asset_id)),
            ).fetchone())
        finally:
            conn.close()

    def get_email_by_imap_uid(self, account_id: int, *, mailbox: str, uid: str,
                              uidvalidity: Optional[str | int] = None) -> Optional[Dict[str, Any]]:
        """Epoch-aware lookup for IMAP ingestion/reconciliation."""
        conn = self.db.connect()
        try:
            return _row(conn.execute("""SELECT * FROM emails WHERE account_id = ? AND mailbox = ?
                                      AND uidvalidity = ? AND uid = ?""",
                                     (int(account_id), mailbox.strip() or "INBOX", self._uidvalidity_key(uidvalidity), str(uid).strip())).fetchone())
        finally:
            conn.close()

    def list_labeled_emails(self, *, category: str, days: int = 7, account_ids: Optional[Sequence[int]] = None,
                            limit: int = 5, offset: int = 0) -> List[Dict[str, Any]]:
        cutoff = _now() - (max(1, int(days)) * 24 * 3600)
        where: List[str] = ["llm_category = ?", "llm_labeled_at IS NOT NULL", "llm_labeled_at >= ?"]
        values: List[Any] = [(category or "").strip().lower(), cutoff]
        if account_ids is not None:
            normalized = [int(value) for value in dict.fromkeys(account_ids)]
            if not normalized:
                return []
            where.append("account_id IN (" + ", ".join("?" for _ in normalized) + ")")
            values.extend(normalized)
        values.extend([max(1, int(limit)), max(0, int(offset))])
        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM emails WHERE {' AND '.join(where)} ORDER BY llm_labeled_at DESC, id DESC LIMIT ? OFFSET ?", tuple(values)).fetchall()]
        finally:
            conn.close()

    # Drafts
    def create_draft(self, account_id: int, *, draft_type: str = "compose", thread_id: Optional[int] = None, from_identity_email: Optional[str] = None,
                     subject: Optional[str] = None, body_markdown: Optional[str] = None, status: str = "open", migration_hold: bool = False) -> Dict[str, Any]:
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            account = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if account is None:
                raise ValueError("draft account does not exist")
            if thread_id is not None:
                thread = conn.execute(
                    "SELECT 1 FROM mail_threads WHERE id = ? AND account_id = ? AND status = 'active'",
                    (int(thread_id), int(account_id)),
                ).fetchone()
                if thread is None:
                    raise ValueError("draft thread does not belong to account")
            cur = conn.execute("""INSERT INTO drafts(account_id, thread_id, draft_type, from_identity_email, subject, body_markdown, status, migration_hold, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (int(account_id), thread_id, draft_type, from_identity_email, subject, body_markdown, status, int(migration_hold), now, now))
            return _row(conn.execute("SELECT * FROM drafts WHERE id = ?", (cur.lastrowid,)).fetchone()) or {}

    def update_draft(self, *, draft_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update v2 draft fields and recipient lists as one short transaction."""
        allowed = {"from_identity_email", "subject", "body_markdown", "status"}
        fields = {key: value for key, value in updates.items() if key in allowed}
        recipient_updates = {key: value for key, value in updates.items() if key in {"to_addrs", "cc_addrs", "bcc_addrs"}}
        now = int(updates.get("_updated_at") or _now())
        with self.db.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM drafts WHERE id = ?", (int(draft_id),)).fetchone() is None:
                return None
            if fields:
                fields["updated_at"] = now
                assignments = ", ".join(f"{key} = ?" for key in fields)
                conn.execute(f"UPDATE drafts SET {assignments} WHERE id = ?", tuple(fields.values()) + (int(draft_id),))
            if recipient_updates:
                current = {kind: [] for kind in ("to", "cc", "bcc")}
                for row in conn.execute("SELECT recipient_type, email FROM draft_recipients WHERE draft_id = ? ORDER BY position, id", (int(draft_id),)):
                    current[row["recipient_type"]].append(row["email"])
                for field, kind in (("to_addrs", "to"), ("cc_addrs", "cc"), ("bcc_addrs", "bcc")):
                    if field in recipient_updates:
                        current[kind] = [part.strip().lower() for part in str(recipient_updates[field] or "").replace(";", ",").split(",") if part.strip()]
                conn.execute("DELETE FROM draft_recipients WHERE draft_id = ?", (int(draft_id),))
                for kind in ("to", "cc", "bcc"):
                    for position, email in enumerate(dict.fromkeys(current[kind])):
                        conn.execute("INSERT INTO draft_recipients(draft_id, recipient_type, email, position) VALUES (?, ?, ?, ?)",
                                     (int(draft_id), kind, email, position))
                if not fields:
                    conn.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (now, int(draft_id)))
            return _row(conn.execute("SELECT * FROM drafts WHERE id = ?", (int(draft_id),)).fetchone())

    def delete_draft(self, draft_id: int) -> bool:
        """Delete a draft and its only in-scope local attachment files.

        Attachment metadata is deleted by SQLite's FK cascade.  Files are kept
        outside the database, so clean them after commit and never resolve a
        stored path outside this draft's own attachment directory.
        """
        draft_key = int(draft_id)
        with self.db.transaction(immediate=True) as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT local_path FROM draft_attachments WHERE draft_id = ?", (draft_key,)
            ).fetchall()]
            deleted = conn.execute("DELETE FROM drafts WHERE id = ?", (draft_key,)).rowcount == 1
        if not deleted:
            return False
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        attachment_root = (data_root / "attachments" / str(draft_key)).resolve()
        for row in rows:
            local_path = row.get("local_path")
            if not local_path:
                continue
            candidate = (data_root / str(local_path)).resolve()
            if candidate.is_relative_to(attachment_root):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            attachment_root.rmdir()
        except OSError:
            pass
        return True

    def replace_draft_recipients(self, draft_id: int, recipients: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self.db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM draft_recipients WHERE draft_id = ?", (int(draft_id),))
            for position, recipient in enumerate(recipients):
                kind = str(recipient.get("type", recipient.get("recipient_type", "to"))).lower()
                if kind not in {"to", "cc", "bcc"}:
                    raise ValueError("recipient type is not valid")
                conn.execute("INSERT OR IGNORE INTO draft_recipients(draft_id, recipient_type, email, display_name, position) VALUES (?, ?, ?, ?, ?)",
                             (int(draft_id), kind, str(recipient["email"]).strip().lower(), recipient.get("display_name"), position))
            return [dict(row) for row in conn.execute("SELECT * FROM draft_recipients WHERE draft_id = ? ORDER BY recipient_type, position, id", (int(draft_id),)).fetchall()]

    def add_draft_attachment(self, draft_id: int, *, file_name: str, file_id: Optional[int] = None, remote_id: Optional[str] = None,
                             file_type: Optional[str] = None, mime_type: Optional[str] = None, size: Optional[int] = None,
                             availability: str = "available", local_path: Optional[str] = None,
                             status: Optional[str] = None, sha256: Optional[str] = None) -> Dict[str, Any]:
        if availability not in {"available", "legacy_missing"}:
            raise ValueError("attachment availability is not valid")
        attachment_status = status or availability
        if attachment_status not in {"available", "legacy_missing"}:
            raise ValueError("attachment status is not valid")
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("""INSERT INTO draft_attachments(draft_id, file_id, remote_id, file_type, file_name, mime_type, size, availability, local_path, status, sha256, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (int(draft_id), file_id, remote_id, file_type, file_name, mime_type, size, availability,
                                local_path, attachment_status, sha256, now, now))
            return _row(conn.execute("SELECT * FROM draft_attachments WHERE id = ?", (cur.lastrowid,)).fetchone()) or {}

    def list_draft_attachments(self, draft_id: int) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM draft_attachments WHERE draft_id = ? ORDER BY id", (int(draft_id),)
            ).fetchall()]
        finally:
            conn.close()

    def get_draft_attachment(self, draft_id: int, attachment_id: int) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            return _row(conn.execute(
                "SELECT * FROM draft_attachments WHERE draft_id = ? AND id = ?", (int(draft_id), int(attachment_id))
            ).fetchone())
        finally:
            conn.close()

    def delete_draft_attachment(self, draft_id: int, attachment_id: int) -> bool:
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("DELETE FROM draft_attachments WHERE draft_id = ? AND id = ?", (int(draft_id), int(attachment_id)))
            return cur.rowcount == 1

    # Idempotent worker operations
    def create_send_operation(self, account_id: int, idempotency_key: str, *, draft_id: Optional[int] = None) -> Dict[str, Any]:
        return self._create_operation("send", account_id, idempotency_key, draft_id=draft_id)

    def create_delete_operation(self, account_id: int, idempotency_key: str, *, email_id: Optional[int] = None,
                                thread_id: Optional[int] = None,
                                provider_mailbox: Optional[str] = None, provider_uid: Optional[str] = None,
                                telegram_chat_id: Optional[int] = None, telegram_message_thread_id: Optional[int] = None) -> Dict[str, Any]:
        return self._create_operation("delete", account_id, idempotency_key, email_id=email_id, thread_id=thread_id,
                                      provider_mailbox=provider_mailbox, provider_uid=provider_uid,
                                      telegram_chat_id=telegram_chat_id, telegram_message_thread_id=telegram_message_thread_id)

    def _create_operation(self, kind: str, account_id: int, idempotency_key: str, **foreign: Optional[int]) -> Dict[str, Any]:
        table = self._OPERATION_CONFIG[kind][0]
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency key is required")
        now = _now()
        field = "draft_id" if kind == "send" else "email_id"
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(f"SELECT * FROM {table} WHERE account_id = ? AND idempotency_key = ?", (int(account_id), key)).fetchone()
            if existing is not None:
                if kind == "delete" and existing["thread_id"] is not None:
                    self._freeze_delete_thread(
                        conn, int(account_id), int(existing["thread_id"]), now
                    )
                return dict(existing)
            if kind == "delete":
                mapping = self._resolve_delete_mapping(conn, int(account_id), foreign)
                cur = conn.execute("""INSERT INTO delete_operations(account_id, email_id, thread_id, provider_mailbox, provider_uid, provider_uidvalidity, telegram_chat_id, telegram_message_thread_id, idempotency_key, created_at, updated_at)
                                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                   (int(account_id), foreign.get("email_id"), mapping["thread_id"], mapping["provider_mailbox"], mapping["provider_uid"], mapping["provider_uidvalidity"],
                                    mapping["telegram_chat_id"], mapping["telegram_message_thread_id"], key, now, now))
                if mapping["thread_id"] is not None:
                    # Freeze the thread in the same IMMEDIATE transaction as
                    # the immutable target snapshot.  A projection that races
                    # this operation can no longer attach a newly received
                    # message to the thread being deleted.
                    self._freeze_delete_thread(
                        conn, int(account_id), int(mapping["thread_id"]), now
                    )
                self._snapshot_delete_targets(conn, int(cur.lastrowid), int(account_id), foreign.get("email_id"), mapping["thread_id"], mapping, now)
            else:
                cur = conn.execute(f"INSERT INTO {table}(account_id, {field}, idempotency_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                                   (int(account_id), foreign.get(field), key, now, now))
            return _row(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (cur.lastrowid,)).fetchone()) or {}

    @staticmethod
    def _resolve_delete_mapping(conn: sqlite3.Connection, account_id: int, supplied: Dict[str, Any]) -> Dict[str, Any]:
        result = {key: supplied.get(key) for key in ("thread_id", "provider_mailbox", "provider_uid", "provider_uidvalidity", "telegram_chat_id", "telegram_message_thread_id")}
        email_id = supplied.get("email_id")
        if email_id is not None:
            row = conn.execute("""SELECT e.thread_id, e.mailbox, e.uid, e.uidvalidity, e.direction, t.telegram_chat_id, t.telegram_message_thread_id
                              FROM emails e LEFT JOIN mail_threads t ON t.id = e.thread_id
                              WHERE e.id = ? AND e.account_id = ?""", (int(email_id), account_id)).fetchone()
            if row is not None:
                result["thread_id"] = result["thread_id"] if result["thread_id"] is not None else row["thread_id"]
                if str(row["direction"] or "incoming") != "outgoing":
                    result["provider_mailbox"] = result["provider_mailbox"] if result["provider_mailbox"] is not None else row["mailbox"]
                    result["provider_uid"] = result["provider_uid"] if result["provider_uid"] is not None else row["uid"]
                    result["provider_uidvalidity"] = result["provider_uidvalidity"] if result["provider_uidvalidity"] is not None else row["uidvalidity"]
                result["telegram_chat_id"] = result["telegram_chat_id"] if result["telegram_chat_id"] is not None else row["telegram_chat_id"]
                result["telegram_message_thread_id"] = result["telegram_message_thread_id"] if result["telegram_message_thread_id"] is not None else row["telegram_message_thread_id"]
        if result["thread_id"] is not None and (result["telegram_chat_id"] is None or result["telegram_message_thread_id"] is None):
            thread = conn.execute("SELECT telegram_chat_id, telegram_message_thread_id FROM mail_threads WHERE id = ? AND account_id = ?",
                                  (int(result["thread_id"]), account_id)).fetchone()
            if thread is not None:
                result["telegram_chat_id"] = result["telegram_chat_id"] if result["telegram_chat_id"] is not None else thread["telegram_chat_id"]
                result["telegram_message_thread_id"] = result["telegram_message_thread_id"] if result["telegram_message_thread_id"] is not None else thread["telegram_message_thread_id"]
        return result

    @staticmethod
    def _snapshot_delete_targets(conn: sqlite3.Connection, operation_id: int, account_id: int, email_id: Optional[int],
                                 thread_id: Optional[int], fallback: Dict[str, Any], now: int) -> None:
        if thread_id is not None:
            # Outgoing rows are local sent-history projections. Their synthetic
            # ``SENT/outgoing:*`` UIDs are not valid IMAP targets and must never
            # be handed to the provider delete worker.
            rows = conn.execute("SELECT id, mailbox, uid, uidvalidity FROM emails WHERE account_id = ? AND thread_id = ? AND direction = 'incoming' AND tombstoned_at IS NULL ORDER BY id",
                                (account_id, int(thread_id))).fetchall()
            for row in rows:
                conn.execute("""INSERT OR IGNORE INTO delete_operation_targets(delete_operation_id, account_id, email_id, provider_mailbox, provider_uid, provider_uidvalidity, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (operation_id, account_id, int(row["id"]), row["mailbox"], row["uid"], row["uidvalidity"], now, now))
        elif email_id is not None:
            direction = conn.execute("SELECT direction FROM emails WHERE id = ?", (int(email_id),)).fetchone()
            if direction is not None and str(direction["direction"] or "incoming") == "outgoing":
                return
            conn.execute("""INSERT OR IGNORE INTO delete_operation_targets(delete_operation_id, account_id, email_id, provider_mailbox, provider_uid, provider_uidvalidity, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (operation_id, account_id, int(email_id), fallback.get("provider_mailbox"), fallback.get("provider_uid"), fallback.get("provider_uidvalidity"), now, now))

    @staticmethod
    def _freeze_delete_thread(conn: sqlite3.Connection, account_id: int, thread_id: int, now: int) -> None:
        """Prevent new mail projections from reusing a thread under deletion."""

        conn.execute(
            """UPDATE mail_threads SET status = 'deleting', updated_at = ?
               WHERE id = ? AND account_id = ? AND status = 'active'""",
            (now, int(thread_id), int(account_id)),
        )

    def get_operation(self, kind: str, operation_id: int) -> Optional[Dict[str, Any]]:
        table = self._OPERATION_CONFIG[kind][0]
        conn = self.db.connect()
        try:
            return _row(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(operation_id),)).fetchone())
        finally:
            conn.close()

    def claim_operation(self, kind: str, operation_id: int) -> Optional[Dict[str, Any]]:
        table, working, _done = self._OPERATION_CONFIG[kind]
        return self.compare_and_set_operation(kind, operation_id, ("queued", "failed"), working, increment_attempt=True)

    def finish_operation(self, kind: str, operation_id: int, *, success: bool, error_code: Optional[str] = None,
                         error_message: Optional[str] = None, provider_message_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        _table, working, done = self._OPERATION_CONFIG[kind]
        state = done if success else "failed"
        extra: Dict[str, Any] = {"error_code": None if success else error_code, "error_message": None if success else error_message}
        if kind == "send" and provider_message_id is not None:
            extra["provider_message_id"] = provider_message_id
        if success:
            extra["completed_at"] = _now()
        return self.compare_and_set_operation(kind, operation_id, (working,), state, extra=extra)

    def compare_and_set_operation(self, kind: str, operation_id: int, expected_statuses: Sequence[str], new_status: str, *,
                                  increment_attempt: bool = False, extra: Optional[Dict[str, Any]] = None,
                                  lease_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        table, working, done = self._OPERATION_CONFIG[kind]
        valid = {"queued", working, done, "failed"}
        if kind == "send":
            valid.add("ambiguous")
        if new_status not in valid or not expected_statuses or any(status not in valid for status in expected_statuses):
            raise ValueError("operation status is not valid")
        now = _now()
        fields: Dict[str, Any] = {"status": new_status, "updated_at": now}
        if increment_attempt:
            fields["attempt_count"] = None
        if extra:
            allowed = {"error_code", "error_message", "completed_at", "provider_message_id"}
            if kind == "delete":
                allowed.update({"provider_deleted", "topic_delete_requested", "topic_deleted", "tombstoned"})
            fields.update({key: value for key, value in extra.items() if key in allowed and (key != "provider_message_id" or kind == "send")})
        placeholders = ", ".join("?" for _ in expected_statuses)
        with self.db.transaction(immediate=True) as conn:
            assignments: List[str] = []
            values: List[Any] = []
            for name, value in fields.items():
                if name == "attempt_count":
                    assignments.append("attempt_count = attempt_count + 1")
                elif kind == "delete" and name in {
                    "provider_deleted", "topic_delete_requested", "topic_deleted", "tombstoned",
                }:
                    # Delete phase flags are monotonic.  The UI may persist an
                    # optimistic Topic phase from a stale snapshot while a
                    # worker commits the provider phase concurrently; merging
                    # at SQL level prevents that stale write from regressing
                    # durable progress.
                    assignments.append(f"{name} = MAX({name}, ?)")
                    values.append(value)
                else:
                    assignments.append(f"{name} = ?")
                    values.append(value)
            predicate = f"id = ? AND status IN ({placeholders})"
            predicate_values: tuple[Any, ...] = (int(operation_id),) + tuple(expected_statuses)
            token = str(lease_token or "").strip()
            if token:
                predicate += " AND lease_token = ? AND lease_until IS NOT NULL AND lease_until >= ?"
                predicate_values += (token, now)
            cur = conn.execute(f"UPDATE {table} SET {', '.join(assignments)} WHERE {predicate}",
                               tuple(values) + predicate_values)
            if cur.rowcount != 1:
                return None
            return _row(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(operation_id),)).fetchone())

    # Worker-facing compatibility names.  They deliberately use the same
    # BEGIN IMMEDIATE CAS primitive as the HTTP-facing methods above.
    def enqueue_send(self, account_id: int, idempotency_key: str, *, draft_id: Optional[int] = None) -> Dict[str, Any]:
        return self.create_send_operation(account_id, idempotency_key, draft_id=draft_id)

    def get_send(self, operation_id: int) -> Optional[Dict[str, Any]]:
        return self.get_operation("send", operation_id)

    def claim_send(self, operation_id: int, *, lease_token: Optional[str] = None, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        return self._claim_operation_with_lease("send", operation_id, lease_token=lease_token, lease_seconds=lease_seconds)

    def claim_next_send(self, *, account_id: Optional[int] = None, lease_token: Optional[str] = None, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        return self._claim_next_operation("send", account_id=account_id, lease_token=lease_token, lease_seconds=lease_seconds)

    def complete_send(self, operation_id: int, *, lease_token: str, success: bool, error_code: Optional[str] = None,
                      error_message: Optional[str] = None, provider_message_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Finish only the active send lease; a stale worker cannot win the CAS."""
        token = lease_token.strip()
        if not token:
            raise ValueError("lease token is required")
        now = _now()
        state = "sent" if success else "failed"
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("""UPDATE send_operations
                                  SET status = ?, provider_message_id = ?, error_code = ?, error_message = ?, completed_at = ?,
                                      lease_token = NULL, lease_until = NULL, updated_at = ?
                                  WHERE id = ? AND status = 'sending' AND lease_token = ? AND lease_until >= ?""",
                               (state, provider_message_id if success else None, None if success else error_code,
                                None if success else error_message, now if success else None, now, int(operation_id), token, now))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM send_operations WHERE id = ?", (int(operation_id),)).fetchone())

    def complete_send_with_outgoing(self, operation_id: int, *, lease_token: str,
                                    provider_message_id: str | None,
                                    outgoing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Atomically finalize SMTP delivery and persist its local history.

        SMTP acknowledgement and the outgoing projection are one logical state
        transition.  Keeping both writes in one SQLite transaction prevents a
        process crash (or a recorder exception) from leaving a durable ``sent``
        operation with no local history row.
        """
        token = str(lease_token or "").strip()
        if not token:
            raise ValueError("lease token is required")
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("""UPDATE send_operations
                                  SET status = 'sent', provider_message_id = ?, error_code = NULL,
                                      error_message = NULL, completed_at = ?, lease_token = NULL,
                                      lease_until = NULL, updated_at = ?
                                  WHERE id = ? AND status = 'sending' AND lease_token = ? AND lease_until >= ?""",
                               (provider_message_id, now, now, int(operation_id), token, now))
            if cur.rowcount != 1:
                return None

            account_id = int(outgoing["account_id"])
            message_id = outgoing.get("message_id") or provider_message_id
            existing = None
            if message_id:
                existing = conn.execute(
                    "SELECT * FROM emails WHERE account_id = ? AND message_id = ? AND direction = 'outgoing' ORDER BY id DESC LIMIT 1",
                    (account_id, str(message_id)),
                ).fetchone()
            if existing is None:
                cur_email = conn.execute(
                    """INSERT INTO emails(account_id, thread_id, mailbox, uidvalidity, uid, message_id,
                              sender, recipient, cc, bcc, subject, body_text, body_html,
                              in_reply_to, references_header, direction, email_date, created_at, updated_at)
                       VALUES (?, ?, 'SENT', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'outgoing', ?, ?, ?)""",
                    (account_id,
                     int(outgoing["thread_id"]) if outgoing.get("thread_id") is not None else None,
                     f"outgoing:{uuid.uuid4().hex}", message_id, outgoing.get("sender"),
                     outgoing.get("recipient"), outgoing.get("cc"), outgoing.get("bcc"),
                     outgoing.get("subject"), outgoing.get("body_text"), outgoing.get("body_html"),
                     outgoing.get("in_reply_to"), outgoing.get("references_header"), outgoing.get("email_date"),
                     now, now),
                )
                outgoing_id = int(cur_email.lastrowid)
            else:
                outgoing_id = int(existing["id"])
            thread_id = outgoing.get("thread_id")
            if thread_id is not None:
                self._refresh_thread_latest(conn, int(thread_id), now=now)
            return _row(conn.execute("SELECT * FROM send_operations WHERE id = ?", (int(operation_id),)).fetchone())

    def mark_reconciled_sent_with_outgoing(self, operation_id: int, *, provider_message_id: str | None,
                                           outgoing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Finalize an ambiguous send and its history in one transaction."""
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("""UPDATE send_operations
                                  SET status = 'sent', provider_message_id = COALESCE(?, provider_message_id),
                                      error_code = NULL, error_message = NULL, completed_at = ?,
                                      lease_token = NULL, lease_until = NULL, updated_at = ?
                                  WHERE id = ? AND status = 'ambiguous'""",
                               (provider_message_id, now, now, int(operation_id)))
            if cur.rowcount != 1:
                return None
            account_id = int(outgoing["account_id"])
            message_id = outgoing.get("message_id") or provider_message_id
            existing = None
            if message_id:
                existing = conn.execute(
                    "SELECT * FROM emails WHERE account_id = ? AND message_id = ? AND direction = 'outgoing' ORDER BY id DESC LIMIT 1",
                    (account_id, str(message_id)),
                ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO emails(account_id, thread_id, mailbox, uidvalidity, uid, message_id,
                              sender, recipient, cc, bcc, subject, body_text, body_html,
                              in_reply_to, references_header, direction, email_date, created_at, updated_at)
                       VALUES (?, ?, 'SENT', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'outgoing', ?, ?, ?)""",
                    (account_id,
                     int(outgoing["thread_id"]) if outgoing.get("thread_id") is not None else None,
                     f"outgoing:{uuid.uuid4().hex}", message_id, outgoing.get("sender"),
                     outgoing.get("recipient"), outgoing.get("cc"), outgoing.get("bcc"),
                     outgoing.get("subject"), outgoing.get("body_text"), outgoing.get("body_html"),
                     outgoing.get("in_reply_to"), outgoing.get("references_header"), outgoing.get("email_date"),
                     now, now),
                )
            thread_id = outgoing.get("thread_id")
            if thread_id is not None:
                self._refresh_thread_latest(conn, int(thread_id), now=now)
            return _row(conn.execute("SELECT * FROM send_operations WHERE id = ?", (int(operation_id),)).fetchone())

    def mark_reconciled_sent(self, operation_id: int, *, provider_message_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.compare_and_set_operation("send", operation_id, ("queued", "sending", "failed", "ambiguous"), "sent",
                                              extra={"provider_message_id": provider_message_id, "completed_at": _now(), "error_code": None, "error_message": None})

    def set_send_state(self, operation_id: int, new_status: str, *, expected_statuses: Sequence[str] = ("queued", "sending", "failed", "ambiguous"),
                       error_code: Optional[str] = None, error_message: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.compare_and_set_operation("send", operation_id, expected_statuses, new_status,
                                              extra={"error_code": error_code, "error_message": error_message,
                                                     "completed_at": _now() if new_status == "sent" else None})

    def enqueue_delete(self, account_id: int, idempotency_key: str, *, email_id: Optional[int] = None,
                       thread_id: Optional[int] = None,
                       provider_mailbox: Optional[str] = None, provider_uid: Optional[str] = None,
                       telegram_chat_id: Optional[int] = None, telegram_message_thread_id: Optional[int] = None) -> Dict[str, Any]:
        return self.create_delete_operation(account_id, idempotency_key, email_id=email_id, thread_id=thread_id, provider_mailbox=provider_mailbox,
                                            provider_uid=provider_uid, telegram_chat_id=telegram_chat_id,
                                            telegram_message_thread_id=telegram_message_thread_id)

    def get_delete_target(self, operation_id: int) -> Optional[Dict[str, Any]]:
        operation = self.get_delete(operation_id)
        if operation is None:
            return None
        conn = self.db.connect()
        try:
            provider_mappings = [dict(row) for row in conn.execute("""SELECT account_id, email_id, provider_mailbox, provider_uid, provider_deleted, provider_attempt_count, last_error
                FROM delete_operation_targets WHERE delete_operation_id = ? ORDER BY email_id""", (int(operation_id),)).fetchall()]
        finally:
            conn.close()
        result = {key: operation[key] for key in ("account_id", "email_id", "thread_id", "provider_mailbox", "provider_uid", "telegram_chat_id", "telegram_message_thread_id")}
        result["provider_mappings"] = provider_mappings
        result["topic"] = {"telegram_chat_id": operation["telegram_chat_id"], "telegram_message_thread_id": operation["telegram_message_thread_id"]}
        return result

    def list_delete_targets(self, operation_id: int, *, pending_only: bool = False) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        try:
            where = "WHERE delete_operation_id = ?" + (" AND provider_deleted = 0" if pending_only else "")
            return [dict(row) for row in conn.execute(f"SELECT * FROM delete_operation_targets {where} ORDER BY email_id", (int(operation_id),)).fetchall()]
        finally:
            conn.close()

    def mark_delete_target_provider_deleted(self, operation_id: int, email_id: int, *, success: bool,
                                            error_message: Optional[str] = None,
                                            lease_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Persist one provider deletion so a resumed saga never repeats it."""
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            token = str(lease_token or "").strip()
            if token:
                active = conn.execute(
                    """SELECT 1 FROM delete_operations
                       WHERE id = ? AND status = 'deleting' AND lease_token = ?
                         AND lease_until IS NOT NULL AND lease_until >= ?""",
                    (int(operation_id), token, now),
                ).fetchone()
                if active is None:
                    return None
            cur = conn.execute("""UPDATE delete_operation_targets SET provider_deleted = MAX(provider_deleted, ?),
                                provider_attempt_count = provider_attempt_count + 1, last_error = ?, updated_at = ?
                                WHERE delete_operation_id = ? AND email_id = ?""",
                               (int(success), None if success else error_message, now, int(operation_id), int(email_id)))
            if cur.rowcount != 1:
                return None
            remaining = conn.execute("SELECT COUNT(*) FROM delete_operation_targets WHERE delete_operation_id = ? AND provider_deleted = 0", (int(operation_id),)).fetchone()[0]
            if int(remaining) == 0:
                conn.execute("UPDATE delete_operations SET provider_deleted = 1, updated_at = ? WHERE id = ?", (now, int(operation_id)))
            return _row(conn.execute("SELECT * FROM delete_operation_targets WHERE delete_operation_id = ? AND email_id = ?",
                                     (int(operation_id), int(email_id))).fetchone())

    def tombstone_thread(self, thread_id: int, *, delete_operation_id: int,
                         lease_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Finalize a delete saga by atomically tombstoning the thread and local mails."""
        now = _now()
        attachments: list[tuple[int, str | None]] = []
        inline_assets: list[tuple[int, str | None]] = []
        with self.db.transaction(immediate=True) as conn:
            operation = conn.execute("SELECT * FROM delete_operations WHERE id = ? AND thread_id = ?", (int(delete_operation_id), int(thread_id))).fetchone()
            if operation is None:
                return None
            if not bool(operation["provider_deleted"]) or not bool(operation["topic_deleted"]):
                raise ValueError("provider and topic deletion must complete before tombstoning")
            if operation["status"] not in {"deleting", "failed", "deleted"}:
                return None
            token = str(lease_token or "").strip()
            if token and (
                operation["status"] != "deleting"
                or operation["lease_token"] != token
                or operation["lease_until"] is None
                or int(operation["lease_until"]) < now
            ):
                return None
            if conn.execute("SELECT 1 FROM mail_threads WHERE id = ?", (int(thread_id),)).fetchone() is None:
                return None
            attachments = [
                (int(row["email_id"]), row["local_path"])
                for row in conn.execute(
                    """SELECT a.email_id, a.local_path FROM email_attachments a
                       JOIN emails e ON e.id = a.email_id
                       WHERE e.thread_id = ?""",
                    (int(thread_id),),
                ).fetchall()
            ]
            inline_assets = [
                (int(row["email_id"]), row["local_path"])
                for row in conn.execute(
                    """SELECT a.email_id, a.local_path FROM email_inline_assets a
                       JOIN emails e ON e.id = a.email_id
                       WHERE e.thread_id = ?""",
                    (int(thread_id),),
                ).fetchall()
            ]
            conn.execute("UPDATE emails SET tombstoned_at = COALESCE(tombstoned_at, ?), updated_at = ? WHERE thread_id = ?", (now, now, int(thread_id)))
            conn.execute("UPDATE mail_threads SET status = 'tombstoned', latest_email_id = NULL, latest_at = NULL, updated_at = ? WHERE id = ?", (now, int(thread_id)))
            if attachments:
                conn.execute(
                    "DELETE FROM email_attachments WHERE email_id IN (SELECT id FROM emails WHERE thread_id = ?)",
                    (int(thread_id),),
                )
            if inline_assets:
                conn.execute(
                    "DELETE FROM email_inline_assets WHERE email_id IN (SELECT id FROM emails WHERE thread_id = ?)",
                    (int(thread_id),),
                )
            conn.execute("""UPDATE delete_operations SET tombstoned = 1, status = 'deleted', completed_at = COALESCE(completed_at, ?),
                            lease_token = NULL, lease_until = NULL, updated_at = ? WHERE id = ?""", (now, now, int(delete_operation_id)))
            result = _row(conn.execute("SELECT * FROM delete_operations WHERE id = ?", (int(delete_operation_id),)).fetchone())
        # Physical files are outside SQLite's transaction. Only reach this
        # cleanup after all provider/Telegram phases and the tombstone commit
        # succeeded; failed deletes intentionally retain their local copies.
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        for email_id, local_path in attachments:
            attachment_root = (data_root / "email-attachments" / str(email_id)).resolve()
            if local_path:
                candidate = (data_root / str(local_path)).resolve()
                if candidate.is_relative_to(attachment_root):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
            _cleanup_email_attachment_root(data_root, email_id)
        for email_id, local_path in inline_assets:
            inline_root = (data_root / "inline-assets" / str(email_id)).resolve()
            if local_path:
                candidate = (data_root / str(local_path)).resolve()
                if candidate.is_relative_to(inline_root):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
            _cleanup_email_file_root(data_root, "inline-assets", email_id)
        return result

    def tombstone_email_for_delete(self, email_id: int, delete_operation_id: int,
                                   *, lease_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Tombstone a threadless email and finish its delete lease atomically.

        The lease and phase checks happen in the same ``BEGIN IMMEDIATE``
        transaction as the local email write.  An expired worker therefore
        cannot mutate the email before discovering that a newer worker owns the
        operation.
        """

        token = str(lease_token or "").strip()
        if not token:
            return None
        now = _now()
        attachments: list[tuple[int, str | None]] = []
        inline_assets: list[tuple[int, str | None]] = []
        with self.db.transaction(immediate=True) as conn:
            operation = conn.execute(
                "SELECT * FROM delete_operations WHERE id = ?",
                (int(delete_operation_id),),
            ).fetchone()
            if operation is None or operation["status"] != "deleting":
                return None
            if (
                operation["lease_token"] != token
                or operation["lease_until"] is None
                or int(operation["lease_until"]) < now
                or not bool(operation["provider_deleted"])
                or not bool(operation["topic_deleted"])
            ):
                return None

            email = conn.execute(
                "SELECT thread_id FROM emails WHERE id = ? AND account_id = ?",
                (int(email_id), int(operation["account_id"])),
            ).fetchone()
            if email is None:
                return None

            attachments = [
                (int(row["email_id"]), row["local_path"])
                for row in conn.execute(
                    "SELECT email_id, local_path FROM email_attachments WHERE email_id = ?",
                    (int(email_id),),
                ).fetchall()
            ]
            inline_assets = [
                (int(row["email_id"]), row["local_path"])
                for row in conn.execute(
                    "SELECT email_id, local_path FROM email_inline_assets WHERE email_id = ?",
                    (int(email_id),),
                ).fetchall()
            ]

            conn.execute(
                "UPDATE emails SET tombstoned_at = COALESCE(tombstoned_at, ?), updated_at = ? WHERE id = ?",
                (now, now, int(email_id)),
            )
            thread_id = email["thread_id"]
            if thread_id is not None:
                self._refresh_thread_latest(conn, int(thread_id), now=now)
                remaining = conn.execute(
                    "SELECT 1 FROM emails WHERE thread_id = ? AND tombstoned_at IS NULL LIMIT 1",
                    (int(thread_id),),
                ).fetchone()
                if remaining is None:
                    conn.execute(
                        "UPDATE mail_threads SET status = 'tombstoned', latest_email_id = NULL, latest_at = NULL, updated_at = ? WHERE id = ?",
                        (now, int(thread_id)),
                    )

            updated = conn.execute(
                """UPDATE delete_operations
                   SET tombstoned = MAX(tombstoned, 1), status = 'deleted',
                       error_code = NULL, error_message = NULL,
                       completed_at = COALESCE(completed_at, ?),
                       lease_token = NULL, lease_until = NULL, updated_at = ?
                   WHERE id = ? AND status = 'deleting' AND lease_token = ?
                     AND lease_until IS NOT NULL AND lease_until >= ?""",
                (now, now, int(delete_operation_id), token, now),
            )
            if updated.rowcount != 1:
                return None
            result = _row(conn.execute(
                "SELECT * FROM delete_operations WHERE id = ?",
                (int(delete_operation_id),),
            ).fetchone())
            conn.execute("DELETE FROM email_attachments WHERE email_id = ?", (int(email_id),))
            conn.execute("DELETE FROM email_inline_assets WHERE email_id = ?", (int(email_id),))
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        for attachment_email_id, local_path in attachments:
            attachment_root = (data_root / "email-attachments" / str(attachment_email_id)).resolve()
            if local_path:
                candidate = (data_root / str(local_path)).resolve()
                if candidate.is_relative_to(attachment_root):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
            _cleanup_email_attachment_root(data_root, attachment_email_id)
        for asset_email_id, local_path in inline_assets:
            asset_root = (data_root / "inline-assets" / str(asset_email_id)).resolve()
            if local_path:
                candidate = (data_root / str(local_path)).resolve()
                if candidate.is_relative_to(asset_root):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
            _cleanup_email_file_root(data_root, "inline-assets", asset_email_id)
        return result

    def get_delete(self, operation_id: int) -> Optional[Dict[str, Any]]:
        return self.get_operation("delete", operation_id)

    def claim_delete(self, operation_id: int, *, lease_token: Optional[str] = None, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        return self._claim_operation_with_lease("delete", operation_id, lease_token=lease_token, lease_seconds=lease_seconds)

    def claim_next_delete(self, *, account_id: Optional[int] = None, lease_token: Optional[str] = None, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        return self._claim_next_operation("delete", account_id=account_id, lease_token=lease_token, lease_seconds=lease_seconds)

    def update_delete(self, operation_id: int, *, success: Optional[bool] = None, status: Optional[str] = None,
                      provider_deleted: Optional[bool] = None, topic_delete_requested: Optional[bool] = None,
                      topic_deleted: Optional[bool] = None, tombstoned: Optional[bool] = None,
                      error_code: Optional[str] = None, error_message: Optional[str] = None,
                      lease_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Persist a delete phase; terminal ``deleted`` requires all three flags."""
        current = self.get_delete(operation_id)
        if current is None:
            return None
        phases = {"provider_deleted": provider_deleted, "topic_deleted": topic_deleted, "tombstoned": tombstoned}
        # Phase flags are monotonic. A stale worker or a best-effort UI update
        # must never turn a completed phase back into pending work.
        merged = {
            key: bool(current[key]) or bool(value)
            for key, value in phases.items()
        }
        extra = {key: int(value) for key, value in merged.items()}
        if topic_delete_requested is not None:
            extra["topic_delete_requested"] = int(bool(current.get("topic_delete_requested")) or bool(topic_delete_requested))
        desired = status or ("deleted" if success is True else ("failed" if success is False else current["status"]))
        if desired == "deleted" and not all(merged.values()):
            raise ValueError("delete cannot be completed before all phases finish")
        extra.update({"error_code": None if desired == "deleted" else error_code, "error_message": None if desired == "deleted" else error_message})
        if desired == "deleted":
            extra["completed_at"] = _now()
        # ``updated_at`` records the failure time.  The claim predicate applies
        # the exponential delay from ``attempt_count`` so the schedule survives
        # process restarts without another mutable timer column.
        return self.compare_and_set_operation(
            "delete", operation_id, ("queued", "deleting", "failed"), desired,
            extra=extra, lease_token=lease_token,
        )

    def _mark_delete_flag(self, operation_id: int, column: str) -> Optional[Dict[str, Any]]:
        """Set one UI-owned delete flag without changing operation scheduling."""

        if column not in {"topic_delete_requested", "topic_deleted"}:
            raise ValueError("unsupported delete marker")
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                f"""UPDATE delete_operations
                    SET {column} = MAX({column}, 1)
                    WHERE id = ? AND status IN ('queued', 'deleting', 'failed')""",
                (int(operation_id),),
            )
            if cur.rowcount != 1:
                return None
            return _row(conn.execute(
                "SELECT * FROM delete_operations WHERE id = ?",
                (int(operation_id),),
            ).fetchone())

    def mark_delete_topic_deleted(self, operation_id: int) -> Optional[Dict[str, Any]]:
        """Persist an optimistic Telegram Topic deletion before mailbox work."""

        return self._mark_delete_flag(int(operation_id), "topic_deleted")

    def mark_delete_topic_requested(self, operation_id: int) -> Optional[Dict[str, Any]]:
        """Persist that the Telegram UI explicitly requested Topic removal."""

        return self._mark_delete_flag(int(operation_id), "topic_delete_requested")

    def _claim_operation_with_lease(self, kind: str, operation_id: int, *, lease_token: Optional[str], lease_seconds: int) -> Optional[Dict[str, Any]]:
        table, working, _done = self._OPERATION_CONFIG[kind]
        now, lease_until = _now(), _now() + max(1, int(lease_seconds))
        token = lease_token or uuid.uuid4().hex
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(operation_id),)).fetchone()
            if row is None:
                return None
            reclaimable_expired = row["status"] == working and (row["lease_until"] is None or int(row["lease_until"]) <= now)
            if row["status"] not in {"queued", "failed"} and not reclaimable_expired:
                return None
            if kind == "delete":
                retryable_failed = (
                    int(row["updated_at"] or 0)
                    + _delete_retry_delay_seconds(int(row["attempt_count"] or 0))
                    <= now
                )
                if row["status"] == "failed" and not retryable_failed:
                    return None
                claim_predicate = f"(status = 'queued' OR (status = 'failed' AND {_DELETE_RETRY_DUE_SQL}) OR (status = ? AND (lease_until IS NULL OR lease_until <= ?)))"
                claim_args = (now, working, now)
            else:
                claim_predicate = "(status IN ('queued', 'failed') OR (status = ? AND (lease_until IS NULL OR lease_until <= ?)))"
                claim_args = (working, now)
            cur = conn.execute(f"""UPDATE {table} SET status = ?, attempt_count = attempt_count + 1, lease_token = ?, lease_until = ?, updated_at = ?
                                WHERE id = ? AND {claim_predicate}""",
                               (working, token, lease_until, now, int(operation_id), *claim_args))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(operation_id),)).fetchone())

    def _claim_next_operation(self, kind: str, *, account_id: Optional[int], lease_token: Optional[str], lease_seconds: int) -> Optional[Dict[str, Any]]:
        table, working, _done = self._OPERATION_CONFIG[kind]
        now, lease_until = _now(), _now() + max(1, int(lease_seconds))
        token = lease_token or uuid.uuid4().hex
        with self.db.transaction(immediate=True) as conn:
            if kind == "delete":
                predicate = f"(status = 'queued' OR (status = 'failed' AND {_DELETE_RETRY_DUE_SQL}) OR (status = ? AND (lease_until IS NULL OR lease_until <= ?)))"
                args: List[Any] = [now, working, now]
            else:
                predicate = "(status IN ('queued', 'failed') OR (status = ? AND (lease_until IS NULL OR lease_until <= ?)))"
                args = [working, now]
            if account_id is not None:
                predicate += " AND account_id = ?"
                args.append(int(account_id))
            candidate = conn.execute(
                f"""SELECT id FROM {table} WHERE {predicate}
                    ORDER BY CASE status
                        WHEN 'queued' THEN 0
                        WHEN 'failed' THEN 2
                        ELSE 1
                    END,
                    updated_at, id
                    LIMIT 1""",
                tuple(args),
            ).fetchone()
            if candidate is None:
                return None
            if kind == "delete":
                claim_predicate = f"(status = 'queued' OR (status = 'failed' AND {_DELETE_RETRY_DUE_SQL}) OR (status = ? AND (lease_until IS NULL OR lease_until <= ?)))"
                claim_args = (now, working, now)
            else:
                claim_predicate = "(status IN ('queued', 'failed') OR (status = ? AND (lease_until IS NULL OR lease_until <= ?)))"
                claim_args = (working, now)
            cur = conn.execute(f"""UPDATE {table} SET status = ?, attempt_count = attempt_count + 1, lease_token = ?, lease_until = ?, updated_at = ?
                                WHERE id = ? AND {claim_predicate}""",
                               (working, token, lease_until, now, int(candidate["id"]), *claim_args))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(candidate["id"]),)).fetchone())

    # Ingestion is leased separately from update processing so that multiple
    # workers can safely share a database while an IMAP account has one poller.
    def acquire_ingestion_lease(self, account_id: int, owner_token: str, *, ttl_seconds: int = 60) -> bool:
        owner = owner_token.strip()
        if not owner:
            raise ValueError("lease owner token is required")
        now = _now()
        expires = now + max(1, int(ttl_seconds))
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT owner_token, expires_at FROM ingestion_leases WHERE account_id = ?", (int(account_id),)).fetchone()
            if row is not None and int(row["expires_at"]) > now and row["owner_token"] != owner:
                return False
            conn.execute("""INSERT INTO ingestion_leases(account_id, owner_token, expires_at, updated_at) VALUES (?, ?, ?, ?)
                            ON CONFLICT(account_id) DO UPDATE SET owner_token=excluded.owner_token, expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                         (int(account_id), owner, expires, now))
            return True

    def release_ingestion_lease(self, account_id: int, owner_token: str) -> bool:
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("DELETE FROM ingestion_leases WHERE account_id = ? AND owner_token = ?", (int(account_id), owner_token))
            return cur.rowcount == 1

    def insert_incoming_if_absent(self, account_id: int, *, mailbox: str, uid: str, uidvalidity: Optional[str | int] = None,
                                  **email: Any) -> Dict[str, Any]:
        """Insert an IMAP message once, returning its durable row on retries."""
        normalized_mailbox, normalized_uid = mailbox.strip() or "INBOX", str(uid).strip()
        normalized_uidvalidity = self._uidvalidity_key(uidvalidity)
        if not normalized_uid:
            raise ValueError("incoming email UID is required")
        now = _now()
        permitted = {
            "thread_id", "message_id", "sender", "recipient", "cc", "bcc", "subject", "email_date",
            "body_text", "body_html", "delivered_to", "in_reply_to", "references_header", "llm_category",
            "llm_priority", "llm_confidence", "llm_labeled_at", "llm_summary", "llm_important_links_json",
            "important_links", "urls",
        }
        values = {key: value for key, value in email.items() if key in permitted}
        links_value = values.pop("important_links", _UNSET)
        if links_value is _UNSET:
            links_value = values.pop("urls", _UNSET)
        if links_value is not _UNSET:
            values["llm_important_links_json"] = _encode_important_links(links_value)
        elif "llm_important_links_json" in values:
            values["llm_important_links_json"] = _encode_important_links(values["llm_important_links_json"])
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute("SELECT * FROM emails WHERE account_id = ? AND mailbox = ? AND uidvalidity = ? AND uid = ?", (int(account_id), normalized_mailbox, normalized_uidvalidity, normalized_uid)).fetchone()
            if existing is not None:
                result = dict(existing)
                result["is_new"] = False
                return result
            # A caller may carry a thread id resolved before a concurrent
            # delete was created. Never persist that stale association: the
            # projection worker will resolve/create a fresh active thread.
            requested_thread_id = values.get("thread_id")
            if requested_thread_id is not None:
                active_thread = conn.execute(
                    "SELECT 1 FROM mail_threads WHERE id = ? AND account_id = ? AND status = 'active'",
                    (int(requested_thread_id), int(account_id)),
                ).fetchone()
                if active_thread is None:
                    values["thread_id"] = None
            columns = ["account_id", "mailbox", "uid", "uidvalidity", "direction", "created_at", "updated_at", *values.keys()]
            args = [int(account_id), normalized_mailbox, normalized_uid, normalized_uidvalidity, "incoming", now, now, *values.values()]
            marks = ", ".join("?" for _ in columns)
            cur = conn.execute(f"INSERT INTO emails({', '.join(columns)}) VALUES ({marks})", tuple(args))
            result = _row(conn.execute("SELECT * FROM emails WHERE id = ?", (cur.lastrowid,)).fetchone()) or {}
            result["is_new"] = True
            if values.get("thread_id") is not None:
                self._refresh_thread_latest(conn, int(values["thread_id"]), now=now)
            return result

    def insert_outgoing_email(
        self, account_id: int, *, thread_id: int | None = None, message_id: str | None = None,
        sender: str | None = None, recipient: str | None = None, cc: str | None = None,
        bcc: str | None = None, subject: str | None = None, body_text: str | None = None,
        body_html: str | None = None, in_reply_to: str | None = None,
        references_header: str | None = None, email_date: str | None = None,
    ) -> Dict[str, Any]:
        """Persist a sent message and refresh its thread's latest projection."""

        now = _now()
        with self.db.transaction(immediate=True) as conn:
            if message_id:
                existing = conn.execute(
                    "SELECT * FROM emails WHERE account_id = ? AND message_id = ? AND direction = 'outgoing' ORDER BY id DESC LIMIT 1",
                    (int(account_id), str(message_id)),
                ).fetchone()
                if existing is not None:
                    return dict(existing)
            cur = conn.execute(
                """INSERT INTO emails(account_id, thread_id, mailbox, uidvalidity, uid, message_id,
                          sender, recipient, cc, bcc, subject, body_text, body_html,
                          in_reply_to, references_header, direction, email_date, created_at, updated_at)
                   VALUES (?, ?, 'SENT', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'outgoing', ?, ?, ?)""",
                (int(account_id), int(thread_id) if thread_id is not None else None,
                 f"outgoing:{uuid.uuid4().hex}", message_id, sender, recipient, cc, bcc,
                 subject, body_text, body_html, in_reply_to, references_header, email_date, now, now),
            )
            email_id = int(cur.lastrowid)
            if thread_id is not None:
                self._refresh_thread_latest(conn, int(thread_id), now=now)
            return _row(conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()) or {}

    def resolve_thread(self, account_id: int, *, message_id: Optional[str] = None, in_reply_to: Optional[str] = None,
                       references_header: Optional[str] = None, subject: Optional[str] = None) -> Optional[Dict[str, Any]]:
        candidates = [value.strip() for value in (in_reply_to, message_id) if value and value.strip()]
        if references_header:
            candidates.extend(part for part in references_header.replace("\n", " ").split() if part)
        conn = self.db.connect()
        try:
            for candidate in candidates:
                row = conn.execute("""SELECT t.* FROM emails e JOIN mail_threads t ON t.id = e.thread_id
                                      WHERE e.account_id = ? AND e.message_id = ? AND t.status = 'active' ORDER BY e.id DESC LIMIT 1""",
                                   (int(account_id), candidate)).fetchone()
                if row is not None:
                    return dict(row)
            if subject:
                normalized = _normalize_thread_subject(subject)
                if not normalized:
                    return None
                row = conn.execute("SELECT * FROM mail_threads WHERE account_id = ? AND subject_normalized = ? AND status = 'active' ORDER BY updated_at DESC, id DESC LIMIT 1",
                                   (int(account_id), normalized)).fetchone()
                return _row(row)
            return None
        finally:
            conn.close()

    def assign_thread(self, email_id: int, thread_id: Optional[int] = None, *, account_id: Optional[int] = None,
                      root_message_id: Optional[str] = None, subject: Optional[str] = None,
                      telegram_chat_id: Optional[int] = None, telegram_message_thread_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Assign a thread once; if omitted, create/reuse a thread for the email."""
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            email = conn.execute("SELECT * FROM emails WHERE id = ?", (int(email_id),)).fetchone()
            if email is None:
                return None
            owner = int(email["account_id"])
            if account_id is not None and int(account_id) != owner:
                raise ValueError("email does not belong to account")
            if email["thread_id"] is not None:
                attached = conn.execute("SELECT * FROM mail_threads WHERE id = ? AND account_id = ? AND status = 'active'", (email["thread_id"], owner)).fetchone()
                if attached is not None:
                    self._refresh_thread_latest(conn, int(attached["id"]), now=now)
                    return _row(attached)
                conn.execute("UPDATE emails SET thread_id = NULL, updated_at = ? WHERE id = ?", (now, int(email_id)))
                email = conn.execute("SELECT * FROM emails WHERE id = ?", (int(email_id),)).fetchone()
            if thread_id is None:
                root = str(root_message_id or email["message_id"] or "").strip() or None
                # A missing Message-ID is not a stable identity.  Never reuse a
                # NULL-root thread: otherwise unrelated no-ID messages collapse
                # into whichever thread happened to be created first.
                existing = None
                if root:
                    existing = conn.execute("SELECT * FROM mail_threads WHERE account_id = ? AND root_message_id = ? AND status = 'active'", (owner, root)).fetchone()
                if existing is None:
                    cur = conn.execute("""INSERT INTO mail_threads(account_id, subject_normalized, root_message_id,
                                        telegram_chat_id, telegram_message_thread_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                       (owner, _normalize_thread_subject(subject or email["subject"]) or None, root,
                                        telegram_chat_id, telegram_message_thread_id, now, now))
                    thread_id = int(cur.lastrowid)
                else:
                    thread_id = int(existing["id"])
            elif conn.execute("SELECT 1 FROM mail_threads WHERE id = ? AND account_id = ? AND status = 'active'", (int(thread_id), owner)).fetchone() is None:
                return None
            cur = conn.execute("UPDATE emails SET thread_id = ?, updated_at = ? WHERE id = ? AND thread_id IS NULL", (int(thread_id), now, int(email_id)))
            if cur.rowcount != 1:
                return None
            conn.execute("""UPDATE mail_threads SET telegram_chat_id = COALESCE(?, telegram_chat_id),
                            telegram_message_thread_id = COALESCE(?, telegram_message_thread_id), updated_at = ? WHERE id = ?""",
                         (telegram_chat_id, telegram_message_thread_id, now, int(thread_id)))
            self._refresh_thread_latest(conn, int(thread_id), now=now)
            return _row(conn.execute("SELECT * FROM mail_threads WHERE id = ?", (int(thread_id),)).fetchone())

    @staticmethod
    def _refresh_thread_latest(conn: sqlite3.Connection, thread_id: int, *, now: int | None = None) -> None:
        timestamp = _now() if now is None else int(now)
        latest = conn.execute(
            """SELECT id, created_at FROM emails
               WHERE thread_id = ? AND tombstoned_at IS NULL
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (int(thread_id),),
        ).fetchone()
        conn.execute(
            """UPDATE mail_threads SET latest_email_id = ?, latest_at = ?, updated_at = ? WHERE id = ?""",
            (latest["id"] if latest else None, latest["created_at"] if latest else None, timestamp, int(thread_id)),
        )

    def tombstone_email(self, email_id: int, *, now: int | None = None) -> Optional[Dict[str, Any]]:
        """Tombstone one message and recompute its thread projection."""

        timestamp = _now() if now is None else int(now)
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT thread_id FROM emails WHERE id = ?", (int(email_id),)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE emails SET tombstoned_at = COALESCE(tombstoned_at, ?), updated_at = ? WHERE id = ?", (timestamp, timestamp, int(email_id)))
            if row["thread_id"] is not None:
                self._refresh_thread_latest(conn, int(row["thread_id"]), now=timestamp)
                remaining = conn.execute("SELECT 1 FROM emails WHERE thread_id = ? AND tombstoned_at IS NULL LIMIT 1", (int(row["thread_id"]),)).fetchone()
                if remaining is None:
                    conn.execute("UPDATE mail_threads SET status = 'tombstoned', latest_email_id = NULL, latest_at = NULL, updated_at = ? WHERE id = ?", (timestamp, int(row["thread_id"])))
            return _row(conn.execute("SELECT * FROM emails WHERE id = ?", (int(email_id),)).fetchone())

    def replace_deleted_topic(
        self,
        thread_id: int,
        *,
        expected_chat_id: int,
        expected_message_thread_id: int,
        new_chat_id: int,
        new_message_thread_id: int,
    ) -> Optional[Dict[str, Any]]:
        """CAS a deleted Topic mapping and requeue each email's primary card.

        A projection job always sends the complete email (primary card/HTML plus
        attachments), so attachment rows must not be queued independently. They
        are discarded here and recreated atomically after the primary delivery
        succeeds on the replacement Topic.
        """

        now = _now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM mail_threads WHERE id = ? AND status = 'active'",
                (int(thread_id),),
            ).fetchone()
            if row is None:
                return None
            matches = (
                row["telegram_chat_id"] == int(expected_chat_id)
                and row["telegram_message_thread_id"] == int(expected_message_thread_id)
            )
            requeued = 0
            if matches:
                conn.execute(
                    """UPDATE mail_threads
                       SET telegram_chat_id = ?, telegram_message_thread_id = ?, updated_at = ?
                       WHERE id = ?""",
                    (int(new_chat_id), int(new_message_thread_id), now, int(thread_id)),
                )
                changed = conn.execute(
                    """UPDATE telegram_delivery_parts
                       SET telegram_chat_id = ?, telegram_message_id = NULL,
                           status = 'queued', lease_token = NULL, lease_until = NULL,
                           topic_delete_markup_version = 0,
                           topic_delete_markup_attempts = 0, updated_at = ?
                       WHERE part_index = 0
                         AND email_id IN (SELECT id FROM emails WHERE thread_id = ?)""",
                    (int(new_chat_id), now, int(thread_id)),
                )
                conn.execute(
                    """DELETE FROM telegram_delivery_parts
                       WHERE part_index > 0
                         AND email_id IN (SELECT id FROM emails WHERE thread_id = ?)""",
                    (int(thread_id),),
                )
                requeued = int(changed.rowcount)
            current = _row(
                conn.execute("SELECT * FROM mail_threads WHERE id = ?", (int(thread_id),)).fetchone()
            )
            if current is None:
                return None
            current["topic_replaced"] = matches
            current["requeued_parts"] = requeued
            return current

    def rollback_new_topic_assignment(self, email_id: int, *, telegram_chat_id: int,
                                      telegram_message_thread_id: int) -> Optional[Dict[str, Any]]:
        """Undo an unprojected, newly-created topic mapping before compensation.

        Existing/shared threads are deliberately left untouched. This method is
        only used after a failed first send, before that topic has a message.
        """
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            email = conn.execute("SELECT thread_id FROM emails WHERE id = ?", (int(email_id),)).fetchone()
            if email is None or email["thread_id"] is None:
                return None
            thread = conn.execute("""SELECT * FROM mail_threads WHERE id = ? AND status = 'active'
                                     AND telegram_chat_id = ? AND telegram_message_thread_id = ?""",
                                  (int(email["thread_id"]), int(telegram_chat_id), int(telegram_message_thread_id))).fetchone()
            if thread is None:
                return None
            references = conn.execute("SELECT COUNT(*) FROM emails WHERE thread_id = ?", (int(thread["id"]),)).fetchone()[0]
            if int(references) != 1:
                return None
            conn.execute("UPDATE emails SET thread_id = NULL, updated_at = ? WHERE id = ? AND thread_id = ?",
                         (now, int(email_id), int(thread["id"])))
            conn.execute("DELETE FROM mail_threads WHERE id = ?", (int(thread["id"]),))
            return dict(thread)

    # Telegram delivery projection: receive mail before an admin has bound a
    # private chat, then atomically make it eligible once one is configured.
    def mark_projection_waiting(self, email_id: int, *, part_index: int = 0) -> Dict[str, Any]:
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM emails WHERE id = ?", (int(email_id),)).fetchone() is None:
                raise KeyError("email does not exist")
            binding = conn.execute("SELECT private_chat_id FROM admin_binding WHERE singleton = 1").fetchone()
            chat_id = binding["private_chat_id"] if binding is not None else None
            status = "queued" if chat_id is not None else "waiting"
            conn.execute("""INSERT INTO telegram_delivery_parts(email_id, part_index, telegram_chat_id, status, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(email_id, part_index) DO UPDATE SET
                              telegram_chat_id = CASE WHEN telegram_delivery_parts.status = 'delivered' THEN telegram_delivery_parts.telegram_chat_id ELSE excluded.telegram_chat_id END,
                              status = CASE WHEN telegram_delivery_parts.status = 'delivered' THEN 'delivered' ELSE excluded.status END,
                              updated_at = excluded.updated_at""",
                         (int(email_id), int(part_index), chat_id, status, now, now))
            return _row(conn.execute("SELECT * FROM telegram_delivery_parts WHERE email_id = ? AND part_index = ?",
                                     (int(email_id), int(part_index))).fetchone()) or {}

    def scan_pending_projections(self, *, limit: int = 100, include_waiting: bool = False) -> List[Dict[str, Any]]:
        statuses = ("waiting", "queued", "failed") if include_waiting else ("queued", "failed")
        marks = ", ".join("?" for _ in statuses)
        conn = self.db.connect()
        try:
            return [dict(row) for row in conn.execute(f"""SELECT * FROM telegram_delivery_parts
                WHERE status IN ({marks}) ORDER BY created_at, id LIMIT ?""", (*statuses, max(1, int(limit)))).fetchall()]
        finally:
            conn.close()

    def replay_pending_projections(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Queue waiting projections after binding; leaves them waiting if unbound."""
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            binding = conn.execute("SELECT private_chat_id FROM admin_binding WHERE singleton = 1").fetchone()
            if binding is None or binding["private_chat_id"] is None:
                return []
            candidates = conn.execute("SELECT id FROM telegram_delivery_parts WHERE status = 'waiting' ORDER BY created_at, id LIMIT ?",
                                      (max(1, int(limit)),)).fetchall()
            ids = [int(row["id"]) for row in candidates]
            if not ids:
                return []
            marks = ", ".join("?" for _ in ids)
            conn.execute(f"UPDATE telegram_delivery_parts SET status = 'queued', telegram_chat_id = ?, updated_at = ? WHERE id IN ({marks}) AND status = 'waiting'",
                         (int(binding["private_chat_id"]), now, *ids))
            return [dict(row) for row in conn.execute(f"SELECT * FROM telegram_delivery_parts WHERE id IN ({marks}) ORDER BY created_at, id", tuple(ids)).fetchall()]

    def reconcile_expired_projections(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Quarantine expired external-send leases for explicit reconciliation.

        A Telegram request may have succeeded just before the worker crashed, so
        an expired ``delivering`` row is never automatically resent.
        """
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            rows = conn.execute("SELECT id FROM telegram_delivery_parts WHERE status = 'delivering' AND (lease_until IS NULL OR lease_until <= ?) ORDER BY updated_at, id LIMIT ?",
                                (now, max(1, int(limit)))).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            marks = ", ".join("?" for _ in ids)
            conn.execute(f"UPDATE telegram_delivery_parts SET status = 'ambiguous', lease_token = NULL, lease_until = NULL, updated_at = ? WHERE id IN ({marks}) AND status = 'delivering'",
                         (now, *ids))
            return [dict(row) for row in conn.execute(f"SELECT * FROM telegram_delivery_parts WHERE id IN ({marks}) ORDER BY id", tuple(ids)).fetchall()]

    def claim_projection(self, part_id: int, *, lease_token: Optional[str] = None, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        token, now = lease_token or uuid.uuid4().hex, _now()
        if not token.strip():
            raise ValueError("projection lease token is required")
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("""UPDATE telegram_delivery_parts SET status = 'delivering', lease_token = ?, lease_until = ?,
                                attempts = attempts + 1, updated_at = ? WHERE id = ? AND status IN ('queued', 'failed')""",
                               (token, now + max(1, int(lease_seconds)), now, int(part_id)))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM telegram_delivery_parts WHERE id = ?", (int(part_id),)).fetchone())

    def claim_next_projection(self, *, lease_token: Optional[str] = None,
                              lease_seconds: int = 60,
                              include_failed: bool = True) -> Optional[Dict[str, Any]]:
        self.reconcile_expired_projections()
        token, now = lease_token or uuid.uuid4().hex, _now()
        if not token.strip():
            raise ValueError("projection lease token is required")
        with self.db.transaction(immediate=True) as conn:
            # Fresh mail must not be starved by one permanently failing older
            # projection. Failed rows remain retryable, but queued work gets the
            # first attempt before retries resume.
            statuses = ("queued", "failed") if include_failed else ("queued",)
            marks = ", ".join("?" for _ in statuses)
            row = conn.execute(f"""SELECT id FROM telegram_delivery_parts
                WHERE status IN ({marks})
                ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, created_at, id
                LIMIT 1""", statuses).fetchone()
            if row is None:
                return None
            cur = conn.execute("""UPDATE telegram_delivery_parts SET status = 'delivering', lease_token = ?, lease_until = ?,
                                attempts = attempts + 1, updated_at = ? WHERE id = ? AND status IN ('queued', 'failed')""",
                               (token, now + max(1, int(lease_seconds)), now, int(row["id"])))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM telegram_delivery_parts WHERE id = ?", (int(row["id"]),)).fetchone())

    def complete_projection(
        self,
        part_id: int,
        *,
        lease_token: str,
        success: bool,
        telegram_message_id: Optional[int] = None,
        message_kind: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        token = lease_token.strip()
        if not token:
            raise ValueError("projection lease token is required")
        normalized_kind = str(message_kind or "").strip().lower() or None
        if normalized_kind not in {None, "html", "text"}:
            raise ValueError("projection message kind is not valid")
        now = _now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("""UPDATE telegram_delivery_parts SET status = ?, telegram_message_id = COALESCE(?, telegram_message_id),
                                  message_kind = COALESCE(?, message_kind),
                                  lease_token = NULL, lease_until = NULL, updated_at = ?
                                  WHERE id = ? AND status = 'delivering' AND lease_token = ? AND lease_until >= ?""",
                               ("delivered" if success else "failed", telegram_message_id if success else None,
                                normalized_kind if success else None, now, int(part_id), token, now))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM telegram_delivery_parts WHERE id = ?", (int(part_id),)).fetchone())

    def mark_projection_ambiguous(self, part_id: int, *, lease_token: str) -> Optional[Dict[str, Any]]:
        """Quarantine a timeout whose Bot API side effect is unknowable."""
        token, now = lease_token.strip(), _now()
        if not token:
            raise ValueError("projection lease token is required")
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("""UPDATE telegram_delivery_parts SET status = 'ambiguous', lease_token = NULL,
                                  lease_until = NULL, updated_at = ? WHERE id = ? AND status = 'delivering'
                                  AND lease_token = ? AND lease_until >= ?""",
                               (now, int(part_id), token, now))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM telegram_delivery_parts WHERE id = ?", (int(part_id),)).fetchone())

    def requeue_projection_after_reconciliation(self, part_id: int) -> Optional[Dict[str, Any]]:
        """Explicitly permit a resend only after the worker reconciles ambiguity."""
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute("UPDATE telegram_delivery_parts SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'ambiguous'",
                               (_now(), int(part_id)))
            if cur.rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM telegram_delivery_parts WHERE id = ?", (int(part_id),)).fetchone())
