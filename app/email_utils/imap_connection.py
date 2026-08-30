"""Database-free IMAP session used by the v2 worker and account verification."""
from __future__ import annotations

from typing import Any

from app.email_utils.connection_factory import ConnectionFactory


class V2IMAPClient:
    """Own only an authenticated IMAP connection; persistence belongs to v2."""

    def __init__(self, account: dict[str, Any]) -> None:
        self.account_info = account
        self.email_addr = str(account.get("email") or "")
        self.conn: Any = None

    def connect(self) -> bool:
        success, _error, connection = ConnectionFactory.try_imap_connection(
            str(self.account_info["imap_server"]),
            int(self.account_info["imap_port"]),
            self.email_addr,
            str(self.account_info.get("password") or ""),
            bool(self.account_info.get("imap_ssl", True)),
        )
        if success:
            self.conn = connection
        return bool(success)

    def disconnect(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.logout()
        finally:
            self.conn = None
