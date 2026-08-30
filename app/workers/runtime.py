"""Optional lifecycle wrapper for running v2 workers from an ASGI lifespan."""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable

from app.integrations.mail.imap import IMAPTransport
from app.integrations.mail.smtp import SMTPTransport
from app.integrations.mail.telegram import MailTelegramProjection
from app.integrations.mail.types import DeleteOperation
from app.services.v2_repository_adapter import V2MailRepositoryAdapter
from app.services.account_verification import AccountDeleteWorker, AccountVerificationWorker
from app.cron.email_receive_config import get_polling_interval_seconds

from .mail import MailDeleteWorker, MailIngestionWorker, MailOutboxWorker, MailProjectionWorker, MailSummaryWorker


class MailWorkerRuntime:
    def __init__(
        self,
        *,
        store: Any | None = None,
        smtp_factory: Any | None = None,
        imap_factory: Any | None = None,
        telegram: Any | None = None,
        accounts: Callable[[], Any] | None = None,
        poll_seconds: float | None = None,
        enable_ingestion: bool = True,
        enable_deletion: bool = True,
        enable_summaries: bool = True,
        llm_summarizer: Any | None = None,
        llm_settings_provider: Any | None = None,
        after_summary_update: Callable[..., Any] | None = None,
        telegram_update_callback: Callable[..., Any] | None = None,
        after_delete: Callable[[], Any] | None = None,
        account_verification: AccountVerificationWorker | None = None,
        account_deletion: AccountDeleteWorker | None = None,
        summary_max_retries: int = 3,
    ) -> None:
        self.store, self.accounts = store, accounts
        self.account_verification = account_verification
        self.account_deletion = account_deletion
        self.poll_seconds = max(float(get_polling_interval_seconds() if poll_seconds is None else poll_seconds), 0.1)
        self.outbox = MailOutboxWorker(store, smtp_factory) if store is not None and smtp_factory is not None else None
        self.summary = MailSummaryWorker(store, telegram, summarizer=llm_summarizer,
                                          settings_provider=llm_settings_provider,
                                          on_updated=after_summary_update or telegram_update_callback,
                                          max_retries=summary_max_retries) if enable_summaries and store is not None else None
        # ``summaries`` is a readable alias retained for callers that prefer a
        # noun over the singular queue name.
        self.summaries = self.summary
        self.ingestion = MailIngestionWorker(store, imap_factory, telegram, summary_worker=self.summary,
                                              llm_summarizer=llm_summarizer,
                                              llm_settings_provider=llm_settings_provider) if enable_ingestion and store is not None and imap_factory is not None and telegram is not None else None
        self.deletion = MailDeleteWorker(store, imap_factory, telegram, after_delete=after_delete) if enable_deletion and store is not None and imap_factory is not None and telegram is not None else None
        self.projection = MailProjectionWorker(store, telegram, imap_factory) if store is not None and telegram is not None else None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._wake_requested = asyncio.Event()
        self._last_cycle_started_at: float | None = None
        self._last_cycle_completed_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error_at: float | None = None
        self._last_error_component: str | None = None
        self._consecutive_failed_cycles = 0
        self._restart_count = 0

    async def start(self) -> None:
        if self._task is None and any((self.outbox, self.ingestion, self.deletion, self.projection, self.summary, self.account_verification, self.account_deletion)):
            self._stopped.clear()
            self._start_supervisor()

    async def stop(self) -> None:
        self._stopped.set()
        self._wake_requested.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def wake(self) -> None:
        """Interrupt the polling delay after an interactive action queues work."""

        self._wake_requested.set()

    def _start_supervisor(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="telegramail-v2-mail-workers")
        self._task.add_done_callback(self._restart_after_unexpected_exit)

    def _restart_after_unexpected_exit(self, task: asyncio.Task[None]) -> None:
        if self._stopped.is_set():
            return
        # The supervisor normally catches ordinary worker failures. This callback
        # covers cancellation or an otherwise unexpected task exit.
        self._restart_count += 1
        self._record_error("supervisor", None)
        try:
            loop = task.get_loop()
            if not loop.is_closed():
                self._start_supervisor()
        except RuntimeError:
            # Event-loop shutdown is handled by the container process itself.
            return

    async def _supervise(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._run()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._restart_count += 1
                self._record_error("supervisor", None)
                await self._wait_for_next_cycle(min(self.poll_seconds, 1.0))
            else:
                if not self._stopped.is_set():
                    # _run is intentionally infinite. If it ever returns, restart
                    # it rather than leaving a seemingly healthy ASGI process.
                    self._restart_count += 1
                    self._record_error("supervisor", None)

    async def _run(self) -> None:
        while not self._stopped.is_set():
            self._last_cycle_started_at = time.monotonic()
            had_errors = False
            if self.outbox:
                had_errors |= not await self._run_isolated("outbox", self.outbox.run_once)
            if self.deletion:
                had_errors |= not await self._drain_deletions()
            if self.account_deletion:
                had_errors |= not await self._run_isolated("account-deletion", self.account_deletion.run_once)
            if self.account_verification:
                had_errors |= not await self._run_isolated("account-verification", self.account_verification.run_once)
            if self.projection:
                had_errors |= not await self._run_isolated("projection-replay", lambda: _maybe_replay_projections(self.store))
                had_errors |= not await self._run_isolated("projection", self.projection.run_once)
            if self.ingestion and self.accounts:
                accounts = await self._get_accounts()
                if accounts is None:
                    had_errors = True
                else:
                    for account in accounts:
                        had_errors |= not await self._run_isolated(
                            "ingestion",
                            lambda account=account: self.ingestion.ingest_account(account),
                        )
            if self.summary:
                had_errors |= not await self._drain_summaries()
            # Ingestion can enqueue an entire mailbox batch. Project every fresh
            # row before sleeping so /inbox never races minutes ahead of Topic
            # creation. Failed rows are excluded here to avoid a tight retry loop;
            # the normal single projection attempt above retries one per cycle.
            if self.projection:
                had_errors |= not await self._drain_fresh_projections()
            self._last_cycle_completed_at = time.monotonic()
            if had_errors:
                self._consecutive_failed_cycles += 1
            else:
                self._consecutive_failed_cycles = 0
                self._last_success_at = self._last_cycle_completed_at
            # Account verification has its own short exponential backoff. Keep
            # the shared supervisor from sleeping past the first 30-second retry
            # when mailbox polling is configured to a much larger interval.
            cycle_wait = min(self.poll_seconds, 30.0) if self.account_verification else self.poll_seconds
            await self._wait_for_next_cycle(cycle_wait)

    async def _get_accounts(self) -> Any | None:
        try:
            result = self.accounts() if self.accounts else ()
            return await result if inspect.isawaitable(result) else result
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_error("accounts", None)
            return None

    async def _run_isolated(self, component: str, action: Callable[[], Any]) -> bool:
        try:
            result = action()
            if inspect.isawaitable(result):
                await result
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_error(component, None)
            return False

    async def _drain_fresh_projections(self, *, limit: int = 100) -> bool:
        try:
            for _ in range(max(1, int(limit))):
                job = self.projection.run_once(include_failed=False)
                if inspect.isawaitable(job):
                    job = await job
                if job is None:
                    break
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_error("projection", None)
            return False

    async def _drain_summaries(self, *, limit: int = 1) -> bool:
        """Process a bounded summary batch without delaying worker readiness."""
        try:
            processed = 0
            for _ in range(max(1, int(limit))):
                job = self.summary.run_once() if self.summary is not None else None
                if inspect.isawaitable(job):
                    job = await job
                if job is None:
                    break
                processed += 1
            if processed == max(1, int(limit)):
                # LLM calls can take tens of seconds. Yield between items so a
                # large backlog cannot keep the first worker cycle unready.
                self.wake()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_error("summary", None)
            return False

    async def _drain_deletions(self, *, limit: int = 3) -> bool:
        """Process a small delete batch without delaying worker readiness."""
        failed_operation_id: Any = None
        processed = 0
        try:
            for _ in range(max(1, int(limit))):
                operation = self.deletion.run_once() if self.deletion is not None else None
                if inspect.isawaitable(operation):
                    operation = await operation
                if operation is None:
                    break
                processed += 1
                operation_id = getattr(operation, "id", None)
                if getattr(operation, "state", None) == "failed":
                    # Compatibility stores may expose a just-failed operation
                    # immediately; do not spin on that same operation.
                    if operation_id == failed_operation_id:
                        break
                    failed_operation_id = operation_id
                else:
                    failed_operation_id = None
            if processed == max(1, int(limit)):
                # Continue a batch promptly instead of waiting for the normal
                # mailbox polling interval, while still yielding a bounded
                # cycle so readiness continues to report real progress.
                self.wake()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_error("delete", None)
            return False

    async def _wait_for_next_cycle(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._wake_requested.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake_requested.clear()

    def _record_error(self, component: str, _: BaseException | None) -> None:
        # Keep health data operational rather than diagnostic: provider exceptions
        # can contain account addresses or transport details.
        self._last_error_component = component
        self._last_error_at = time.monotonic()

    def readiness(self, *, now: float | None = None) -> tuple[bool, dict[str, Any]]:
        """Return an operational readiness view without exposing provider errors."""
        timestamp = time.monotonic() if now is None else float(now)
        task = self._task
        latest = self._last_cycle_completed_at
        payload: dict[str, Any] = {
            "worker_started": task is not None,
            "worker_task_alive": bool(task is not None and not task.done()),
            "consecutive_failed_cycles": self._consecutive_failed_cycles,
            "restart_count": self._restart_count,
            "last_error_component": self._last_error_component,
        }
        if latest is not None:
            payload["last_cycle_age_seconds"] = round(max(0.0, timestamp - latest), 3)
        if task is None:
            return False, {**payload, "reason": "worker_not_started"}
        if task.done():
            return False, {**payload, "reason": "worker_task_exited"}
        if latest is None:
            return False, {**payload, "reason": "worker_waiting_for_first_cycle"}
        if timestamp - latest > max(self.poll_seconds * 3, 30.0):
            return False, {**payload, "reason": "worker_cycle_stale"}
        if self._consecutive_failed_cycles >= 3:
            return False, {**payload, "reason": "worker_repeated_failures"}
        return True, {**payload, "status": "ok"}


def create_worker_runtime(**kwargs: Any) -> MailWorkerRuntime:
    """Create a no-op-safe runtime; deployments supply concrete v2 adapters."""
    return MailWorkerRuntime(**kwargs)


def create_v2_worker_runtime(
    repository: Any,
    telegram: Any,
    *,
    poll_seconds: float | None = None,
    after_telegram_delivery: Callable[[], Any] | None = None,
    after_delete: Callable[[], Any] | None = None,
    llm_summarizer: Any | None = None,
    llm_settings_provider: Any | None = None,
    after_summary_update: Callable[..., Any] | None = None,
    telegram_update_callback: Callable[..., Any] | None = None,
    summary_max_retries: int = 3,
) -> MailWorkerRuntime:
    """Wire ``app.db.V2Repository`` to concrete IMAP/SMTP transports.

    ``telegram`` is intentionally injected so this module has no dependency on a
    particular Telegram HTTP client. Credentials are decrypted only as each
    account is handed to a transport.
    """
    adapter = V2MailRepositoryAdapter(repository)
    if llm_settings_provider is None:
        for name in ("get_global_llm_settings", "get_llm_settings", "load_llm_settings"):
            candidate = getattr(repository, name, None)
            if callable(candidate):
                llm_settings_provider = candidate
                break

    def smtp_factory(draft: Any) -> SMTPTransport:
        if not draft.transport_account:
            raise ValueError("missing SMTP account settings for queued send")
        return SMTPTransport(dict(draft.transport_account))

    def imap_factory(account: Any) -> IMAPTransport:
        if isinstance(account, DeleteOperation):
            # Purge operations soft-delete the account before the provider phase;
            # the worker must still be able to read its transport snapshot while
            # the public account API keeps deleted rows hidden.
            try:
                values = repository.get_account(int(account.account_id), include_deleted=True)
            except TypeError:
                values = repository.get_account(int(account.account_id))
            if values is None:
                raise KeyError("delete operation account no longer exists")
        else:
            values = dict(account)
        values["password"] = repository.get_account_password(int(values["id"]))
        return IMAPTransport(values)

    def verification_imap_factory(account: Any) -> IMAPTransport:
        return IMAPTransport(dict(account))

    def verification_smtp_factory(account: Any) -> SMTPTransport:
        return SMTPTransport(dict(account))

    def accounts() -> list[dict[str, Any]]:
        return [dict(account) for account in repository.list_accounts() if bool(account.get("enabled", True))]

    def bound_private_chat_id(_: Any) -> int | str | None:
        binding = repository.get_admin_binding()
        return binding.get("private_chat_id") if binding else None

    projection = telegram if hasattr(telegram, "ensure_private_topic") else MailTelegramProjection(
        telegram,
        bound_private_chat_id,
        repository=repository,
        after_delivery=after_telegram_delivery,
    )
    summary_callback = after_summary_update or telegram_update_callback
    if summary_callback is None and callable(getattr(projection, "update_mail_summary", None)):
        summary_callback = projection.update_mail_summary
    return MailWorkerRuntime(store=adapter, smtp_factory=smtp_factory, imap_factory=imap_factory, telegram=projection,
                             accounts=accounts, poll_seconds=poll_seconds,
                             account_verification=AccountVerificationWorker(repository, verification_imap_factory, verification_smtp_factory),
                             account_deletion=AccountDeleteWorker(repository, telegram),
                             llm_summarizer=llm_summarizer,
                             llm_settings_provider=llm_settings_provider,
                             after_summary_update=summary_callback,
                             telegram_update_callback=telegram_update_callback,
                             after_delete=after_delete,
                             summary_max_retries=summary_max_retries)


async def _maybe_replay_projections(store: Any) -> None:
    method = getattr(store, "replay_pending_projections", None)
    if method is None:
        return
    result = method()
    if inspect.isawaitable(result):
        await result
