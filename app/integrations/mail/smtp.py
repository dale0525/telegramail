"""Adapter for the existing SMTP implementation, without Telegram dependencies."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

from app.email_utils.smtp_client import SMTPClient
from app.email_utils.connection_factory import ConnectionFactory

from .types import MailDraft


class SMTPUncertainDeliveryError(TimeoutError):
    """The legacy SMTP boundary swallowed an I/O failure after DATA may have run."""


class SMTPTransport:
    """Expose ``send_email`` on top of the mature legacy SMTP client.

    It is intentionally tiny: retry/lease policy belongs to the outbox worker,
    not the transport which cannot know whether a timeout accepted SMTP DATA.
    """
    def __init__(self, account: dict[str, Any], *, client_cls: type[SMTPClient] = SMTPClient) -> None:
        self.account = account
        self.client = client_cls(
            server=account["smtp_server"],
            port=int(account["smtp_port"]),
            username=account.get("email", ""),
            password=account.get("password", ""),
            use_ssl=bool(account.get("smtp_ssl", True)),
        )

    async def send_email(self, **kwargs: Any) -> bool:
        result = await self.client.send_email(**kwargs)
        if result is False:
            # Legacy SMTPClient returns False for every transport exception and
            # loses whether DATA was accepted. Be conservative: do not retry.
            raise SMTPUncertainDeliveryError("legacy SMTP client returned an unclassified failure")
        return bool(result)

    async def verify(self) -> bool:
        """Connect, authenticate, and close SMTP without accepting a message."""
        verifier = getattr(self.client, "verify", None)
        if callable(verifier):
            if inspect.iscoroutinefunction(verifier):
                value = await verifier()
            else:
                value = await asyncio.to_thread(verifier)
            if inspect.isawaitable(value):
                value = await value
            if not value:
                raise ConnectionError("unable to verify SMTP")
            return True
        return await asyncio.to_thread(self._verify_sync)

    def _verify_sync(self) -> bool:
        smtp = ConnectionFactory.create_smtp_connection(
            self.account["smtp_server"], int(self.account["smtp_port"]),
            bool(self.account.get("smtp_ssl", True)), timeout=30,
        )
        try:
            smtp.ehlo()
            if not bool(self.account.get("smtp_ssl", True)):
                smtp.starttls()
                smtp.ehlo()
            smtp.login(self.account.get("email", ""), self.account.get("password", ""))
            smtp.quit()
            return True
        finally:
            # ``quit`` can itself fail after a successful authentication; avoid
            # leaving a socket open while preserving the original exception.
            try:
                smtp.close()
            except Exception:
                pass


class SMTPReconciliationHook:
    """Protocol adapter for a sent-mail Message-ID lookup.

    The lookup callback can use IMAP's sent mailbox or a provider API. Returning
    false keeps an operation ``ambiguous``; this hook never authorizes resend.
    """
    def __init__(self, lookup: Any) -> None:
        self.lookup = lookup

    async def has_message_id(self, message_id: str, operation: Any = None) -> bool:
        result = self.lookup(message_id, operation) if callable(self.lookup) else self.lookup.has_message_id(message_id)
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)
