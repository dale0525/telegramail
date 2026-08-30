"""Small, optional contracts for API integrations.

The API deliberately accepts duck-typed implementations.  These Protocols make
the intended integration points discoverable without imposing a migration on the
existing SQLite manager.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SetupCodeStore(Protocol):
    def consume_setup_code(self, *, code: str, telegram_user_id: int) -> bool: ...


@runtime_checkable
class TelegramUpdateHandler(Protocol):
    def handle_update(self, update: dict[str, Any]) -> Any: ...


@runtime_checkable
class OperationClient(Protocol):
    def send_draft(self, draft: dict[str, Any]) -> Any: ...

    def delete_thread(self, thread_id: str) -> Any: ...
