import asyncio
from typing import Any, Callable

from app.cron.email_check_scheduler import start_email_check_scheduler
from app.cron.email_receive_config import (
    get_imap_idle_fallback_poll_seconds,
    get_imap_idle_reconnect_backoff_seconds,
    get_imap_idle_timeout_seconds,
)
from app.cron.imap_idle_manager import IMAPIdleManager

_current_email_receive_runtime: Any | None = None


class EmailReceiveRuntime:
    def __init__(
        self,
        *,
        mode: str,
        polling_interval_seconds: int,
        idle_manager: Any | None = None,
        polling_starter: Callable[[int], Any] = start_email_check_scheduler,
    ):
        self.mode = mode
        self.polling_interval_seconds = polling_interval_seconds
        self.idle_manager = idle_manager or IMAPIdleManager(
            idle_timeout_seconds=get_imap_idle_timeout_seconds(),
            fallback_poll_seconds=get_imap_idle_fallback_poll_seconds(),
            reconnect_backoff_seconds=get_imap_idle_reconnect_backoff_seconds(),
        )
        self.polling_starter = polling_starter
        self.polling_task: asyncio.Task | None = None

    def start(self) -> None:
        if self.mode in {"polling", "hybrid"}:
            self.polling_task = self.polling_starter(self.polling_interval_seconds)

        if self.mode in {"idle", "hybrid"}:
            self.idle_manager.start()

    async def stop(self) -> None:
        global _current_email_receive_runtime

        if self.polling_task and not self.polling_task.done():
            self.polling_task.cancel()
            await asyncio.gather(self.polling_task, return_exceptions=True)

        if self.mode in {"idle", "hybrid"}:
            await self.idle_manager.stop()

        if _current_email_receive_runtime is self:
            _current_email_receive_runtime = None

    async def remove_account(self, account_id: Any) -> int:
        if self.mode not in {"idle", "hybrid"}:
            return 0

        stop_account = getattr(self.idle_manager, "stop_account", None)
        if stop_account is None:
            return 0
        return await stop_account(account_id)


def get_current_email_receive_runtime() -> EmailReceiveRuntime | None:
    return _current_email_receive_runtime


def start_email_receive_runtime(
    *,
    mode: str,
    polling_interval_seconds: int,
    idle_manager: Any | None = None,
    polling_starter: Callable[[int], Any] = start_email_check_scheduler,
) -> EmailReceiveRuntime:
    global _current_email_receive_runtime

    runtime = EmailReceiveRuntime(
        mode=mode,
        polling_interval_seconds=polling_interval_seconds,
        idle_manager=idle_manager,
        polling_starter=polling_starter,
    )
    runtime.start()
    _current_email_receive_runtime = runtime
    return runtime
