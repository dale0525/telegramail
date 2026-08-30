"""Background verification and cleanup workers for Mini App accounts."""

from __future__ import annotations

import asyncio
import imaplib
import inspect
import smtplib
import socket
import ssl
import uuid
from typing import Any, Callable


def classify_connection_error(exc: BaseException) -> str:
    if isinstance(exc, (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error, PermissionError)):
        return "authentication"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (ConnectionError, ConnectionRefusedError, socket.gaierror, ssl.SSLError, OSError)):
        return "connection"
    return "protocol"


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class AccountVerificationWorker:
    def __init__(self, repository: Any, imap_factory: Callable[[dict[str, Any]], Any], smtp_factory: Callable[[dict[str, Any]], Any]) -> None:
        self.repository = repository
        self.imap_factory = imap_factory
        self.smtp_factory = smtp_factory

    async def run_once(self, *, limit: int = 20) -> bool:
        accounts = self.repository.claim_accounts_for_verification(limit=limit)
        had_error = False
        for account in accounts:
            try:
                password = self.repository.get_account_password(int(account["id"]))
                transport_account = {**account, "password": password}
                imap_transport = self.imap_factory(transport_account)
                smtp_transport = self.smtp_factory(transport_account)

                async def check(transport: Any) -> tuple[bool, str | None]:
                    try:
                        result = await _maybe_await(transport.verify())
                        return (False, "connection") if result is False else (True, None)
                    except Exception as exc:  # provider libraries expose varied exception types
                        return False, classify_connection_error(exc)

                imap_result, smtp_result = await asyncio.gather(check(imap_transport), check(smtp_transport))
                errors = list(dict.fromkeys(value for ok, value in (imap_result, smtp_result) if not ok and value))
                self.repository.record_account_verification(
                    int(account["id"]), ok=not errors, error=";".join(errors) if errors else None,
                )
            except Exception as exc:
                had_error = True
                self.repository.record_account_verification(
                    int(account["id"]), ok=False, error=classify_connection_error(exc),
                )
        return not had_error


class AccountDeleteWorker:
    def __init__(self, repository: Any, telegram: Any, *, worker_id: str | None = None) -> None:
        self.repository = repository
        self.telegram = telegram
        self.worker_id = worker_id or f"account-delete-{uuid.uuid4().hex}"

    async def run_once(self) -> dict[str, Any] | None:
        operation = self.repository.claim_next_account_delete(lease_token=self.worker_id)
        if operation is None:
            return None
        # A crash can occur after the account row is purged but before the
        # operation status is committed.  ON DELETE SET NULL leaves an
        # already-completed operation; finalize it instead of retrying forever.
        raw_account_id = operation.get("account_id")
        if raw_account_id is None:
            return self.repository.update_account_delete_operation(int(operation["id"]), status="deleted")
        account_id = int(raw_account_id)
        try:
            if bool(operation.get("purge_data", 1)):
                for topic in self.repository.list_account_topics(account_id):
                    result = await self._delete_topic(topic)
                    if result is False:
                        raise RuntimeError("Telegram Topic deletion was not confirmed")
                if not self.repository.hard_delete_account(account_id):
                    getter = getattr(self.repository, "get_account", None)
                    remaining = None
                    if callable(getter):
                        try:
                            remaining = getter(account_id, include_deleted=True)
                        except TypeError:
                            remaining = getter(account_id)
                    if remaining is not None:
                        raise RuntimeError("account cleanup was not confirmed")
            else:
                if not self.repository.soft_delete_account(account_id):
                    getter = getattr(self.repository, "get_account", None)
                    remaining = None
                    if callable(getter):
                        try:
                            remaining = getter(account_id, include_deleted=True)
                        except TypeError:
                            remaining = getter(account_id)
                    if not (remaining and remaining.get("deleted_at") is not None):
                        raise RuntimeError("account removal was not confirmed")
            return self.repository.update_account_delete_operation(int(operation["id"]), status="deleted")
        except Exception as exc:
            return self.repository.update_account_delete_operation(int(operation["id"]), status="failed", error=str(exc))

    async def _delete_topic(self, topic: dict[str, Any]) -> Any:
        method = getattr(self.telegram, "delete_forum_topic", None)
        if callable(method):
            try:
                return await _maybe_await(method(topic["telegram_chat_id"], int(topic["telegram_message_thread_id"])))
            except Exception as exc:
                description = str(exc).lower()
                if "thread not found" in description or "topic_id_invalid" in description:
                    return True
                raise
        method = getattr(self.telegram, "delete_topic", None)
        if callable(method):
            return await _maybe_await(method(topic))
        raise RuntimeError("Telegram Topic client is unavailable")
