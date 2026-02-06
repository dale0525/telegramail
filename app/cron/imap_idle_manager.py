import asyncio
from typing import Any, Awaitable, Callable

from app.cron.email_ingestion import fetch_account_emails
from app.email_utils.account_manager import AccountManager
from app.email_utils.imap_client import IMAPClient
from app.email_utils.imap_idle_helper import idle_wait_once, supports_idle
from app.utils import Logger

logger = Logger().get_logger(__name__)


class IMAPIdleManager:
    def __init__(
        self,
        *,
        account_manager: Any | None = None,
        imap_client_cls: Any = IMAPClient,
        supports_idle_fn: Callable[[Any], bool] = supports_idle,
        idle_wait_once_fn: Callable[..., bool] = idle_wait_once,
        fetch_account_emails_fn: Callable[[dict[str, Any]], Awaitable[int]] = fetch_account_emails,
        idle_timeout_seconds: int = 1740,
        fallback_poll_seconds: int = 30,
        reconnect_backoff_seconds: int = 5,
        max_reconnect_backoff_seconds: int = 300,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._account_manager = account_manager or AccountManager()
        self._imap_client_cls = imap_client_cls
        self._supports_idle_fn = supports_idle_fn
        self._idle_wait_once_fn = idle_wait_once_fn
        self._fetch_account_emails_fn = fetch_account_emails_fn
        self._idle_timeout_seconds = max(idle_timeout_seconds, 10)
        self._fallback_poll_seconds = max(fallback_poll_seconds, 1)
        self._reconnect_backoff_seconds = max(reconnect_backoff_seconds, 1)
        self._max_reconnect_backoff_seconds = max(max_reconnect_backoff_seconds, 1)
        self._sleep_fn = sleep_fn
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    @staticmethod
    def _watcher_key(account: dict[str, Any], mailbox: str) -> str:
        return f"{account.get('id')}:{(mailbox or 'INBOX').strip().lower()}"

    def _resolve_mailboxes(self, account: dict[str, Any]) -> list[str]:
        try:
            probe = self._imap_client_cls(account)
            raw_boxes = probe._get_monitored_mailboxes()
        except Exception as e:
            logger.warning(
                f"Failed to resolve monitored mailboxes for {account.get('email')}: {e}"
            )
            raw_boxes = ["INBOX"]

        boxes: list[str] = []
        seen: set[str] = set()
        for box in raw_boxes or ["INBOX"]:
            name = (box or "").strip().strip('"') or "INBOX"
            lowered = name.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            boxes.append(name)
        return boxes or ["INBOX"]

    def start(self) -> None:
        if self._running:
            return

        self._running = True
        accounts = self._account_manager.get_all_accounts()
        for account in accounts:
            for mailbox in self._resolve_mailboxes(account):
                key = self._watcher_key(account, mailbox)
                if key in self._tasks:
                    continue
                self._tasks[key] = asyncio.create_task(
                    self._run_watcher(account, mailbox)
                )
        logger.info(f"Started IMAP IDLE manager with {len(self._tasks)} watcher(s)")

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Stopped IMAP IDLE manager")

    async def _run_watcher(self, account: dict[str, Any], mailbox: str) -> None:
        backoff_seconds = self._reconnect_backoff_seconds
        email_addr = account.get("email", "<unknown>")
        mailbox_name = (mailbox or "").strip().strip('"') or "INBOX"

        while self._running:
            imap_client = self._imap_client_cls(account)
            try:
                if not imap_client.connect():
                    logger.warning(
                        f"IDLE connect failed for {email_addr}/{mailbox_name}, retrying in {backoff_seconds}s"
                    )
                    await self._sleep_fn(backoff_seconds)
                    backoff_seconds = min(
                        backoff_seconds * 2, self._max_reconnect_backoff_seconds
                    )
                    continue

                status, _ = imap_client.conn.select(mailbox_name)
                if status != "OK":
                    logger.warning(
                        f"Failed to select mailbox '{mailbox_name}' for IDLE ({email_addr}), fallback polling in {self._fallback_poll_seconds}s"
                    )
                    await self._fetch_account_emails_fn(account)
                    await self._sleep_fn(self._fallback_poll_seconds)
                    continue

                if not self._supports_idle_fn(imap_client.conn):
                    logger.info(
                        f"IMAP IDLE not supported for {email_addr}/{mailbox_name}; using fallback polling every {self._fallback_poll_seconds}s"
                    )
                    await self._fetch_account_emails_fn(account)
                    await self._sleep_fn(self._fallback_poll_seconds)
                    continue

                backoff_seconds = self._reconnect_backoff_seconds
                changed = self._idle_wait_once_fn(
                    imap_client.conn, timeout_seconds=self._idle_timeout_seconds
                )
                if changed:
                    await self._fetch_account_emails_fn(account)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"IDLE watcher error for {email_addr}/{mailbox_name}: {e}"
                )
                await self._sleep_fn(backoff_seconds)
                backoff_seconds = min(
                    backoff_seconds * 2, self._max_reconnect_backoff_seconds
                )
            finally:
                try:
                    imap_client.disconnect()
                except Exception:
                    pass
