"""Crash-safe v2 mail workers.

Each state transition is a short repository call. SMTP/IMAP/Telegram calls are
made only after a lease has committed and before the terminal state update.
"""
from __future__ import annotations

import asyncio
from collections import deque
import inspect
import logging
import smtplib
import socket
import time
import uuid
from dataclasses import replace
from email.utils import getaddresses
from typing import Any, Callable, Iterable

from app.integrations.mail.compose import MailComposer
from app.integrations.mail.telegram import ProjectionDeliveryError, ProjectionWaitingBinding, TelegramTopic, format_topic_name
from app.integrations.telegram_http import is_missing_forum_topic_error
from app.integrations.mail.types import DeleteOperation, FetchedMessages, IncomingMail, ProjectionJob, SendOperation, SummaryJob
from app.services.account_verification import classify_connection_error
from app.services.telegram_mail_card import has_projectable_mail_content


logger = logging.getLogger(__name__)


def _safe_important_links(value: Any) -> list[dict[str, str]]:
    """Normalize links at every worker boundary, including injected fakes."""

    from app.email_utils.llm import sanitize_important_links

    return sanitize_important_links(value)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _call(target: Any, names: Iterable[str], *args: Any, **kwargs: Any) -> Any:
    """Call the first supported duck-typed API, retaining useful TypeErrors."""
    for name in names:
        method = getattr(target, name, None)
        if method is None:
            continue
        try:
            return await _maybe_await(method(*args, **kwargs))
        except TypeError as exc:
            # Fakes commonly accept a smaller signature. Retry without kwargs only
            # when it is clearly an argument-shape problem.
            try:
                return await _maybe_await(method(*args))
            except TypeError:
                raise exc
    raise AttributeError(f"none of {tuple(names)!r} exists on {target!r}")


async def _record_account_status(store: Any, account_id: Any, *, ok: bool, error: BaseException | str | None = None) -> None:
    """Best-effort activity status update that never changes mail operation semantics."""
    if account_id is None:
        return
    summary = None if ok else (
        classify_connection_error(error) if isinstance(error, BaseException) else str(error or "connection")[:200]
    )
    for owner in (store, getattr(store, "repository", None)):
        method = getattr(owner, "update_account_connection_status", None) if owner is not None else None
        if not callable(method):
            continue
        try:
            value = method(int(account_id), ok=ok, error=summary)
            await _maybe_await(value)
        except Exception:
            # Status telemetry must not turn a completed send/sync into a failure.
            logger.debug("unable to update account activity status", exc_info=True)
        return


class MailOutboxWorker:
    def __init__(self, store: Any, smtp_factory: Any, *, reconciler: Any = None, worker_id: str | None = None, lease_seconds: float = 60.0) -> None:
        self.store, self.smtp_factory, self.reconciler = store, smtp_factory, reconciler
        self.worker_id, self.lease_seconds = worker_id or f"outbox-{uuid.uuid4().hex}", lease_seconds

    async def run_once(self, operation_id: str | None = None) -> SendOperation | None:
        if operation_id is None:
            operation = await _call(self.store, ("claim_next_send",), self.worker_id, self.lease_seconds)
        else:
            operation = await _call(self.store, ("claim_send",), operation_id, self.worker_id, self.lease_seconds)
        if operation is None:
            return None
        return await self._deliver(operation)

    async def _deliver(self, operation: SendOperation) -> SendOperation:
        lease_token = operation.lease_token
        if not lease_token:
            return operation
        try:
            message, message_id, recipients = MailComposer.compose(operation.draft)
            smtp = self.smtp_factory(operation.draft) if callable(self.smtp_factory) else self.smtp_factory
            # Message-capable v2 transports are preferred. Existing SMTPClient is
            # adapted through send_email with the draft's already-stable Message-ID.
            if hasattr(smtp, "send"):
                result = await _call(smtp, ("send",), message, recipients)
            else:
                d = operation.draft
                result = await _call(smtp, ("send_email",), from_email=d.from_email, from_name=d.from_name, to_addrs=list(d.to), cc_addrs=list(d.cc), bcc_addrs=list(d.bcc), subject=d.subject, text_body=d.text_body or d.markdown_body or "", html_body=d.html_body, reply_to=d.reply_to, in_reply_to=d.in_reply_to, references=list(d.references), message_id=message_id, attachments=[{"filename": a.filename, "data": a.data, "mime_type": a.mime_type} for a in d.attachments])
            if result is False:
                raise RuntimeError("SMTP transport rejected delivery")
        except (TimeoutError, asyncio.TimeoutError, socket.timeout, ConnectionError, smtplib.SMTPServerDisconnected) as exc:
            # SMTP can accept data just before a timeout. Do not resend this op.
            await _call(self.store, ("complete_send",), operation.id, lease_token, "ambiguous", error=str(exc))
            await _record_account_status(self.store, getattr(operation.draft, "account_id", None), ok=False, error=exc)
            operation.state, operation.error = "ambiguous", str(exc)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException) as exc:
            # A final SMTP 5xx rejection is definite; it is safe to expose as a
            # failed operation instead of suppressing automatic resend under the
            # ambiguous-delivery policy.
            await _call(self.store, ("complete_send",), operation.id, lease_token, "failed", error=str(exc))
            await _record_account_status(self.store, getattr(operation.draft, "account_id", None), ok=False, error=exc)
            operation.state, operation.error = "failed", str(exc)
        except Exception as exc:
            await _call(self.store, ("complete_send",), operation.id, lease_token, "failed", error=str(exc))
            await _record_account_status(self.store, getattr(operation.draft, "account_id", None), ok=False, error=exc)
            operation.state, operation.error = "failed", str(exc)
        else:
            persisted_draft = replace(operation.draft, message_id=message_id)
            atomic_finalizer = getattr(self.store, "complete_send_with_outgoing", None)
            if callable(atomic_finalizer):
                try:
                    finalized = await _call(
                        self.store, ("complete_send_with_outgoing",), operation.id,
                        lease_token, provider_message_id=message_id, draft=persisted_draft,
                    )
                except Exception as exc:
                    # A database/recorder failure after SMTP success is itself
                    # ambiguous.  Never turn it into a retryable ``failed`` row:
                    # the provider may already have accepted the message.
                    logger.warning("unable to atomically finalize outgoing mail", exc_info=True)
                    await _call(
                        self.store, ("set_send_state",), operation.id, "ambiguous",
                        expected_statuses=("sending",), error=str(exc),
                    )
                    operation.state, operation.error = "ambiguous", str(exc)
                    await _record_account_status(self.store, getattr(operation.draft, "account_id", None), ok=False, error=exc)
                    return operation
                if not finalized:
                    # SMTP already returned success, but this worker no longer
                    # owns the lease. Quarantine only while the row is still in
                    # ``sending``; a concurrent worker may have finalized it.
                    current = await _call(self.store, ("get_send",), operation.id)
                    current_state = getattr(current, "state", None)
                    if isinstance(current, dict):
                        current_state = current.get("status") or current.get("state")
                    if current_state == "sending":
                        await _call(
                            self.store, ("set_send_state",), operation.id, "ambiguous",
                            expected_statuses=("sending",), error="send finalization lease expired",
                        )
                        current = await _call(self.store, ("get_send",), operation.id)
                        current_state = getattr(current, "state", None)
                        if isinstance(current, dict):
                            current_state = current.get("status") or current.get("state")
                    operation.state = "sent" if current_state == "sent" else "ambiguous"
                    operation.error = None if operation.state == "sent" else "send finalization lease expired"
                    if operation.state == "ambiguous":
                        await _record_account_status(self.store, getattr(operation.draft, "account_id", None), ok=False, error=operation.error)
                        return operation
            else:
                completed = await _call(
                    self.store,
                    ("complete_send",),
                    operation.id,
                    lease_token,
                    "sent",
                    provider_message_id=message_id,
                )
                if not completed:
                    operation.state, operation.error = "ambiguous", "send finalization lease expired"
                    await _record_account_status(self.store, getattr(operation.draft, "account_id", None), ok=False, error=operation.error)
                    return operation
                # Keep the thread's materialized latest-email pointer in sync
                # for legacy/custom stores without the atomic finalizer.
                recorder = getattr(self.store, "record_outgoing_email", None)
                if callable(recorder):
                    try:
                        await _maybe_await(recorder(persisted_draft))
                    except Exception:
                        logger.warning("unable to persist outgoing mail projection", exc_info=True)
            await _record_account_status(self.store, getattr(operation.draft, "account_id", None), ok=True)
            operation.state, operation.error, operation.sent_at = "sent", None, time.time()
        return operation

    async def reconcile_ambiguous(self, operation_id: str) -> bool:
        """Explicit reconciliation hook; it never re-sends an ambiguous message."""
        if self.reconciler is None:
            return False
        operation = await _call(self.store, ("get_send",), operation_id)
        if not operation or operation.state != "ambiguous":
            return False
        delivered = await _call(self.reconciler, ("has_message_id", "reconcile"), operation.draft.message_id or MailComposer.compose(operation.draft)[1], operation)
        if delivered:
            message_id = operation.draft.message_id
            if not message_id:
                try:
                    _message, message_id, _recipients = MailComposer.compose(operation.draft)
                except Exception:
                    message_id = None
            # Reconciliation uses a repository-specific terminal update so it does
            # not need to fabricate a stale lease token.
            reconciled_draft = replace(operation.draft, message_id=message_id)
            atomic_reconcile = getattr(self.store, "mark_reconciled_sent_with_outgoing", None)
            if callable(atomic_reconcile):
                await _call(
                    self.store, ("mark_reconciled_sent_with_outgoing",), operation_id,
                    provider_message_id=message_id, draft=reconciled_draft,
                )
            else:
                mark_reconciled = getattr(self.store, "mark_reconciled_sent", None)
                if callable(mark_reconciled):
                    await _call(
                        self.store,
                        ("mark_reconciled_sent",),
                        operation_id,
                        provider_message_id=message_id,
                    )
                else:
                    await _call(self.store, ("set_send_state",), operation_id, "sent")
                recorder = getattr(self.store, "record_outgoing_email", None)
                if callable(recorder):
                    try:
                        await _maybe_await(recorder(reconciled_draft))
                    except Exception:
                        logger.warning("unable to persist reconciled outgoing mail projection", exc_info=True)
            operation.state, operation.reconciled_at = "sent", time.time()
        return bool(delivered)


class MailSummaryWorker:
    """Asynchronous LLM summary/label queue.

    The worker intentionally uses duck-typed repository hooks so deployments
    can add a durable queue without coupling this module to a schema migration.
    When those hooks are absent, a bounded in-process queue still guarantees
    that IMAP ingestion never waits on the model.
    """

    def __init__(
        self,
        store: Any,
        telegram: Any = None,
        *,
        summarizer: Any = None,
        settings_provider: Any = None,
        on_updated: Any = None,
        telegram_update_callback: Any = None,
        on_telegram_update: Any = None,
        worker_id: str | None = None,
        lease_seconds: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self.store = store
        self.telegram = telegram
        self.summarizer = summarizer
        self.settings_provider = settings_provider
        self.on_updated = on_updated or telegram_update_callback or on_telegram_update
        self.worker_id = worker_id or f"summary-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self._pending: deque[SummaryJob] = deque()
        self._retry_at: dict[str, float] = {}
        self._remote_retry_at = 0.0
        self._counter = 0

    @property
    def pending(self) -> tuple[SummaryJob, ...]:
        """Snapshot of locally queued jobs (useful for health/tests)."""
        return tuple(self._pending)

    async def _email_id(self, mail: IncomingMail) -> int | None:
        value = getattr(mail, "email_id", None)
        if value is not None:
            return int(value)
        # V2MailRepositoryAdapter keeps this mapping in-process while ingesting.
        cache = getattr(self.store, "_email_ids", None)
        if isinstance(cache, dict):
            key = (str(mail.account_id), mail.mailbox.casefold(), str(mail.uidvalidity or ""), str(mail.uid))
            if key in cache:
                return int(cache[key])
        lookup, _ = self._optional_method(("get_email_by_imap_uid",))
        if callable(lookup):
            try:
                row = lookup(mail.account_id, mailbox=mail.mailbox, uid=mail.uid, uidvalidity=mail.uidvalidity)
                row = await row if inspect.isawaitable(row) else row
                if row and row.get("id") is not None:
                    return int(row["id"])
            except Exception:
                pass
        return None

    def _optional_method(self, names: Iterable[str]) -> tuple[Any | None, str | None]:
        for owner in (self.store, getattr(self.store, "repository", None)):
            if owner is None:
                continue
            for name in names:
                method = getattr(owner, name, None)
                if callable(method):
                    return method, name
        return None, None

    async def enqueue(self, mail: IncomingMail) -> SummaryJob:
        """Queue one mail and return its pending job without invoking the LLM."""
        self._counter += 1
        job: SummaryJob | Any = None
        method, method_name = self._optional_method(("enqueue_summary", "enqueue_llm_summary", "enqueue_summary_task"))
        _, claim_name = self._optional_method(("claim_next_summary", "claim_next_llm_summary", "claim_summary_task"))
        claim_available = claim_name is not None
        remote_queue = callable(method) and claim_available
        if callable(method):
            try:
                if method_name == "enqueue_summary_task":
                    email_id = await self._email_id(mail)
                    value = method(email_id) if email_id is not None else None
                else:
                    value = method(mail)
                job = await value if inspect.isawaitable(value) else value
            except (AttributeError, TypeError):
                # Optional repository hooks must not make mail receipt fail.
                job = None
        if isinstance(job, SummaryJob):
            normalized = job
        elif isinstance(job, dict) and job.get("mail") is not None:
            state = "pending" if job.get("state") in (None, "queued") else job.get("state")
            normalized = SummaryJob(id=job.get("id", self._counter), mail=job["mail"], attempts=int(job.get("attempts") or 0), state=state)
        elif isinstance(job, dict):
            email_id = job.get("email_id")
            if email_id is not None:
                mail = replace(mail, email_id=int(email_id))
            state = "pending" if job.get("state") in (None, "queued") else job.get("state")
            normalized = SummaryJob(id=int(email_id) if email_id is not None else job.get("id", self._counter), mail=mail, attempts=int(job.get("attempts") or 0), state=state)
        else:
            normalized = SummaryJob(id=job if job not in (None, True, False) else (await self._email_id(mail) or self._counter), mail=mail)
        if not remote_queue or job is None:
            self._pending.append(normalized)
        return normalized

    enqueue_summary = enqueue
    enqueue_pending = enqueue
    submit = enqueue

    async def _load_settings(self) -> Any:
        provider = self.settings_provider
        if provider is None:
            provider, _ = self._optional_method(("get_global_llm_settings", "get_llm_settings", "load_llm_settings"))
        if provider is None:
            return None
        try:
            value = provider() if callable(provider) else provider
            return await value if inspect.isawaitable(value) else value
        except Exception:
            logger.warning("unable to load global LLM settings", exc_info=True)
            return None

    async def _claim_remote(self, include_failed: bool) -> Any:
        if time.monotonic() < self._remote_retry_at:
            return None
        method, method_name = self._optional_method(("claim_next_summary", "claim_next_llm_summary", "claim_summary_task"))
        if not callable(method):
            return None
        try:
            if method_name == "claim_summary_task":
                value = method(lease_seconds=int(self.lease_seconds))
            else:
                value = method(self.worker_id, self.lease_seconds, include_failed=include_failed)
        except TypeError:
            value = method() if method_name == "claim_summary_task" else method(self.worker_id, self.lease_seconds)
        result = await value if inspect.isawaitable(value) else value
        if method_name == "claim_summary_task" and isinstance(result, dict) and result.get("mail") is None:
            email_id = result.get("email_id")
            getter, _ = self._optional_method(("get_email",))
            if email_id is not None and callable(getter):
                row = getter(int(email_id))
                row = await row if inspect.isawaitable(row) else row
                if row:
                    mail = IncomingMail(
                        account_id=row.get("account_id"), mailbox=row.get("mailbox") or "INBOX",
                        uid=str(row.get("uid") or ""), uidvalidity=row.get("uidvalidity") or None,
                        message_id=row.get("message_id"), sender=row.get("sender") or "",
                        to=tuple(filter(None, str(row.get("recipient") or "").split(","))),
                        cc=tuple(filter(None, str(row.get("cc") or "").split(","))),
                        subject=row.get("subject") or "", text_body=row.get("body_text") or "",
                        html_body=row.get("body_html"), received_at=row.get("email_date") or "",
                        summary=row.get("llm_summary"), category=row.get("llm_category"),
                        priority=row.get("llm_priority"),
                        important_links=_safe_important_links(
                            row.get("important_links")
                            if row.get("important_links") is not None
                            else row.get("llm_important_links")
                            if row.get("llm_important_links") is not None
                            else row.get("llm_important_links_json")
                            if row.get("llm_important_links_json") is not None
                            else row.get("urls")
                        ),
                        email_id=int(email_id),
                    )
                    result = {**result, "mail": mail, "id": int(email_id)}
        if isinstance(result, dict) and isinstance(result.get("mail"), IncomingMail):
            mail = result["mail"]
            cleaned_links = _safe_important_links(getattr(mail, "important_links", ()))
            if cleaned_links != getattr(mail, "important_links", None):
                result = {**result, "mail": replace(mail, important_links=cleaned_links)}
        return result

    def _claim_local(self) -> SummaryJob | None:
        now = time.monotonic()
        for _ in range(len(self._pending)):
            job = self._pending.popleft()
            key = str(job.id)
            if now >= self._retry_at.get(key, 0.0):
                self._retry_at.pop(key, None)
                job.state, job.lease_token = "running", self.worker_id
                job.attempts += 1
                return job
            self._pending.append(job)
        return None

    async def _summarize(self, mail: IncomingMail, settings: Any) -> dict[str, Any] | None:
        callback = self.summarizer
        if callback is None:
            from app.email_utils.llm import summarize_email
            return await asyncio.to_thread(
                summarize_email,
                mail.text_body,
                llm_settings=settings,
                stream=True,
                strict_stream=True,
            )
        # Keep injected test/integration callbacks source-compatible with the
        # old one-argument ``summarize(body)`` contract.
        try:
            first_name = next(iter(inspect.signature(callback).parameters), "").lower()
        except (TypeError, ValueError):
            first_name = ""
        first_value = mail if first_name in {"mail", "email", "message", "incoming"} else mail.text_body
        second_value = mail.text_body if first_value is mail else mail
        attempts = (
            lambda: callback(first_value, llm_settings=settings, stream=True),
            lambda: callback(first_value, settings),
            lambda: callback(first_value),
            lambda: callback(second_value),
        )
        for invoke in attempts:
            try:
                value = invoke()
                return await value if inspect.isawaitable(value) else value
            except TypeError:
                continue
        raise TypeError("unsupported summarizer callback signature")

    async def _persist(self, job: SummaryJob, analysis: dict[str, Any]) -> None:
        updated = False
        for name in ("update_incoming_labels", "update_email_labels", "persist_summary", "set_email_summary", "update_summary"):
            method = getattr(self.store, name, None)
            if not callable(method):
                continue
            try:
                value = method(job.mail, analysis)
            except TypeError:
                value = method(job.mail, category=analysis.get("category"), priority=analysis.get("priority"), confidence=analysis.get("category_confidence"), summary=analysis.get("summary"))
            result = await value if inspect.isawaitable(value) else value
            updated = bool(result) or updated
            if result is not False:
                break
        label_method, _ = self._optional_method(("update_email_llm_labels",))
        if not updated and callable(label_method):
            # Repository adapters can expose only an email id based update.  A
            # job may carry that id directly, while custom stores may resolve it
            # from the mail identity.
            email_id = getattr(job.mail, "email_id", None) or (job.id if isinstance(job.id, int) else None)
            if email_id is not None:
                label_kwargs = dict(
                    email_id=int(email_id), category=str(analysis.get("category") or "other"),
                    priority=str(analysis.get("priority") or "medium"),
                    confidence=analysis.get("category_confidence"), summary=analysis.get("summary"),
                    important_links=analysis.get("important_links", analysis.get("urls", ())),
                )
                try:
                    value = label_method(**label_kwargs)
                except TypeError:
                    label_kwargs.pop("important_links", None)
                    value = label_method(**label_kwargs)
                await _maybe_await(value)

    async def _notify(self, job: SummaryJob, analysis: dict[str, Any]) -> None:
        callback = self.on_updated
        if callback is not None:
            try:
                try:
                    result = callback(job.mail, analysis)
                except TypeError:
                    result = callback(job.mail)
                await _maybe_await(result)
            except Exception:
                # Persistence is authoritative.  A transient Telegram edit
                # failure must not turn a successfully generated summary into
                # a model failure or consume its retry budget.
                logger.warning("unable to notify summary update", exc_info=True)
            return
        # Optional Telegram adapters can update an already delivered card/topic
        # in place.  Absence of such a hook is expected during first delivery.
        if self.telegram is None:
            return
        for name in ("update_mail_summary", "update_summary", "refresh_mail", "refresh_projection", "notify_summary", "on_summary_updated"):
            method = getattr(self.telegram, name, None)
            if callable(method):
                try:
                    await _maybe_await(method(job.mail, analysis))
                except TypeError:
                    try:
                        await _maybe_await(method(job.mail))
                    except Exception:
                        logger.warning("unable to notify summary update", exc_info=True)
                except Exception:
                    logger.warning("unable to notify summary update", exc_info=True)
                break

    async def _complete_remote(
        self, job: Any, success: bool, error: str | None = None, *, status: str | None = None
    ) -> None:
        task_method, _ = self._optional_method(("complete_summary_task",))
        if callable(task_method):
            email_id = getattr(getattr(job, "mail", None), "email_id", None) or getattr(job, "id", None)
            if email_id is not None:
                try:
                    value = task_method(int(email_id), success=success, error=error,
                                        status=status, lease_token=getattr(job, "lease_token", None))
                except TypeError:
                    value = task_method(int(email_id), success=success, error=error)
                await _maybe_await(value)
            return
        method, _ = self._optional_method(("complete_summary", "complete_llm_summary"))
        if not callable(method):
            return
        try:
            value = method(job.id, success, analysis=getattr(job, "analysis", None), error=error,
                           status=status, lease_token=getattr(job, "lease_token", None))
        except TypeError:
            try:
                value = method(job, success, error=error)
            except TypeError:
                value = method(job.id, success)
        await _maybe_await(value)

    @staticmethod
    def _settings_allow_summary(settings: Any, body: str) -> bool:
        """Return whether a persisted Mini App configuration can run a job."""
        if not settings:
            return False
        get = settings.get if isinstance(settings, dict) else lambda key: getattr(settings, key, None)
        if not bool(get("enabled") if get("enabled") is not None else get("llm_enabled")):
            return False
        if not (get("api_key") or get("llm_api_key")):
            return False
        if not (get("base_url") or get("llm_base_url")):
            return False
        if not (get("model") or get("llm_model") or get("models")):
            return False
        try:
            threshold = int(get("summary_threshold") if get("summary_threshold") is not None else get("threshold") or 120)
        except (TypeError, ValueError):
            threshold = 120
        from app.email_utils.text import remove_spaces_and_urls
        return len(remove_spaces_and_urls(body)) >= max(0, threshold)

    async def _requeue_remote(self, job: Any) -> None:
        method, name = self._optional_method(("enqueue_summary_task",))
        if name is None or not callable(method):
            return
        email_id = getattr(getattr(job, "mail", None), "email_id", None) or getattr(job, "id", None)
        if email_id is None:
            return
        try:
            value = method(int(email_id), force=True)
        except TypeError:
            value = method(int(email_id))
        await _maybe_await(value)

    async def run_once(self, *, include_failed: bool = True) -> SummaryJob | None:
        remote = await self._claim_remote(include_failed)
        job = remote or self._claim_local()
        if job is None:
            return None
        if not isinstance(job, SummaryJob):
            if isinstance(job, dict) and job.get("mail") is not None:
                job = SummaryJob(id=job.get("id", self._counter), mail=job["mail"], attempts=int(job.get("attempts") or 0), state="running", lease_token=job.get("lease_token"))
            else:
                return None
        is_remote = remote is not None
        if job.attempts <= 0:
            job.attempts = 1
        try:
            settings = await self._load_settings()
            analysis = await self._summarize(job.mail, settings)
            if analysis is not None and not isinstance(analysis, dict):
                raise ValueError("summarizer returned a non-object result")
            if analysis is None and self.summarizer is None and not self._settings_allow_summary(settings, job.mail.text_body):
                # Disabled/missing/short-content jobs are intentionally left
                # as ``skipped``.  The UI maps this internal state to “待生成”
                # without claiming a summary was generated.
                job.state, job.error = "skipped", None
                await self._complete_remote(job, False, status="skipped")
                return job
            if isinstance(analysis, dict):
                raw_links = (
                    analysis["important_links"]
                    if analysis.get("important_links") is not None
                    else analysis.get("urls", getattr(job.mail, "important_links", ()))
                )
                cleaned_links = _safe_important_links(raw_links)
                analysis = dict(analysis)
                analysis["important_links"] = cleaned_links
                # Keep old callback/repository consumers source-compatible while
                # making the canonical value explicit for new adapters.
                analysis["urls"] = list(cleaned_links)
                job.analysis = analysis
                await self._persist(job, analysis)
                job.mail = replace(
                    job.mail,
                    summary=str(analysis.get("summary") or "") or None,
                    category=str(analysis.get("category") or "") or None,
                    priority=str(analysis.get("priority") or "") or None,
                    important_links=cleaned_links,
                )
                await self._notify(job, analysis)
            job.state, job.error = "completed", None
            self._remote_retry_at = 0.0
            await self._complete_remote(job, True)
        except Exception as exc:
            job.state, job.error = "failed", str(exc)
            await self._complete_remote(job, False, str(exc))
            if job.attempts <= self.max_retries:
                await self._requeue_remote(job)
                if is_remote:
                    self._remote_retry_at = time.monotonic() + self.retry_delay * max(1, job.attempts)
                self._retry_at[str(job.id)] = time.monotonic() + self.retry_delay * max(1, job.attempts)
                if not is_remote:
                    self._pending.append(job)
            logger.warning("mail summary failed; queued retry: job=%s attempt=%s", job.id, job.attempts)
        return job


# Concise alias for integrations that call this a summary worker.
SummaryWorker = MailSummaryWorker
MailLLMWorker = MailSummaryWorker
LLMSummaryWorker = MailSummaryWorker
MailSummarizationWorker = MailSummaryWorker


class MailIngestionWorker:
    def __init__(self, store: Any, imap_factory: Any, telegram: Any, *, worker_id: str | None = None, lease_seconds: float = 90.0,
                 llm_summarizer: Any = None, summary_worker: Any = None,
                 llm_settings_provider: Any = None) -> None:
        self.store, self.imap_factory, self.telegram = store, imap_factory, telegram
        self.worker_id, self.lease_seconds = worker_id or f"ingest-{uuid.uuid4().hex}", lease_seconds
        self.llm_summarizer = llm_summarizer
        self.summary_worker = summary_worker
        self.llm_settings_provider = llm_settings_provider

    async def ingest_account(self, account: Any, mailbox: str = "INBOX") -> int:
        account_id = account["id"] if isinstance(account, dict) else getattr(account, "id", account)
        acquired = await _call(self.store, ("acquire_ingestion_lease",), account_id, mailbox, self.worker_id, self.lease_seconds)
        if not acquired:
            return 0
        try:
            client = self.imap_factory(account) if callable(self.imap_factory) else self.imap_factory
            cursor = await _imap_cursor(self.store, account_id, mailbox)
            batch = await _fetch_incremental(client, mailbox, int(cursor.get("last_uid") or 0))
            if batch.uidvalidity and cursor.get("uidvalidity") != batch.uidvalidity:
                # A UID can be reused after UIDVALIDITY changes. A bootstrapped
                # cursor has no prior UIDVALIDITY, so it is also treated as
                # unknown and safely rescanned from UID 1.
                await _call(self.store, ("reset_imap_cursor",), account_id, mailbox, batch.uidvalidity, 0)
                batch = await _fetch_incremental(client, mailbox, 0)
            messages = batch.messages
            count = 0
            for message in messages or ():
                mail = self._coerce_message(message, account_id, mailbox)
                # A UID can be reused in a new UIDVALIDITY epoch. Carry the
                # batch epoch through every persistence/projection key.
                if batch.uidvalidity:
                    mail = replace(mail, uidvalidity=batch.uidvalidity)
                inserted_result = await _call(self.store, ("insert_incoming_if_absent", "insert_email_if_absent"), mail)
                inserted = inserted_result
                if isinstance(inserted_result, dict):
                    inserted = inserted_result.get("is_new", True)
                    if inserted_result.get("id") is not None:
                        mail = replace(mail, email_id=int(inserted_result["id"]))
                if not inserted:
                    await _advance_uid(self.store, account_id, mailbox, batch.uidvalidity, mail.uid)
                    continue
                # LLM work is deliberately detached from IMAP receipt.  A
                # supplied summary worker owns retries and persistence; the
                # synchronous path remains for backwards-compatible callers
                # that inject a summarizer directly.
                if self.summary_worker is not None:
                    await _call(self.summary_worker, ("enqueue", "enqueue_summary"), mail)
                else:
                    mail = await self._label_if_enabled(mail)
                await self._index_contacts(mail)
                # Persist projection intent before any Telegram I/O. The separate
                # projection worker owns topic creation/send retries, so a normal
                # Telegram failure cannot discard an otherwise durable email.
                await _call(self.store, ("enqueue_projection", "mark_projection_waiting"), mail)
                await _advance_uid(self.store, account_id, mailbox, batch.uidvalidity, mail.uid)
                count += 1
            await _record_account_status(self.store, account_id, ok=True)
            return count
        except Exception as exc:
            await _record_account_status(self.store, account_id, ok=False, error=exc)
            raise
        finally:
            await _call(self.store, ("release_ingestion_lease",), account_id, mailbox, self.worker_id)

    def _coerce_message(self, value: Any, account_id: Any, mailbox: str) -> IncomingMail:
        if isinstance(value, IncomingMail):
            cleaned_links = _safe_important_links(getattr(value, "important_links", ()))
            return value if cleaned_links == getattr(value, "important_links", None) else replace(
                value, important_links=cleaned_links
            )
        if isinstance(value, dict):
            copied = dict(value)
            copied.setdefault("account_id", account_id)
            copied.setdefault("mailbox", mailbox)
            for key in ("to", "cc", "references"):
                if isinstance(copied.get(key), list):
                    copied[key] = tuple(copied[key])
            raw_links = (
                copied["important_links"]
                if copied.get("important_links") is not None
                else copied.pop("urls", ())
            )
            copied["important_links"] = _safe_important_links(raw_links)
            return IncomingMail(**copied)
        raise TypeError("IMAP adapter must yield IncomingMail or a matching dict")

    async def _index_contacts(self, mail: IncomingMail) -> None:
        addresses = [mail.sender, *mail.to, *mail.cc]
        for _, address in getaddresses(addresses):
            if address:
                await _call(self.store, ("upsert_contact", "upsert_mail_contact"), mail.account_id, address)

    async def _label_if_enabled(self, mail: IncomingMail) -> IncomingMail:
        """Best-effort compatibility labeling for callers without a queue.

        Production runtime always injects ``MailSummaryWorker``.  This path is
        retained for small integrations/tests that provide an explicit
        summarizer, and it reads provider settings from the repository rather
        than from process environment variables.
        """
        try:
            summarizer = self.llm_summarizer
            if summarizer is None:
                from app.email_utils.llm import summarize_email
                settings = self.llm_settings_provider
                if callable(settings):
                    settings = settings()
                settings = await _maybe_await(settings)
                analysis = await asyncio.to_thread(
                    summarize_email, mail.text_body, llm_settings=settings
                )
            elif inspect.iscoroutinefunction(summarizer):
                analysis = await summarizer(mail.text_body)
            else:
                analysis = await asyncio.to_thread(summarizer, mail.text_body)
            if isinstance(analysis, dict):
                raw_links = (
                    analysis["important_links"]
                    if analysis.get("important_links") is not None
                    else analysis.get("urls", getattr(mail, "important_links", ()))
                )
                cleaned_links = _safe_important_links(raw_links)
                analysis = dict(analysis)
                analysis["important_links"] = cleaned_links
                analysis["urls"] = list(cleaned_links)
                await _call(self.store, ("update_incoming_labels", "update_email_labels"), mail, analysis)
                return replace(mail, summary=str(analysis.get("summary") or "") or None,
                               category=str(analysis.get("category") or "") or None,
                               priority=str(analysis.get("priority") or "") or None,
                               important_links=cleaned_links)
        except Exception:
            # Do not include message content or model output in logs/errors.
            pass
        return mail

    async def _ensure_topic(self, account: Any, mail: IncomingMail) -> Any:
        # The client owns how an account maps to a private Telegram chat. The
        # topic is only created after a genuinely new UID has been persisted.
        return await _call(
            self.telegram,
            ("ensure_private_topic", "ensure_topic", "create_topic"),
            account,
            format_topic_name(mail.sender, mail.subject),
        )

    async def _project(self, mail: IncomingMail, thread_id: Any) -> None:
        await _call(self.telegram, ("project_mail", "send_mail", "send_to_topic"), mail, thread_id)


class MailDeleteWorker:
    """Telegram Topic → provider → local tombstone saga with durable progress."""
    def __init__(self, store: Any, imap_factory: Any, telegram: Any, *, worker_id: str | None = None,
                 lease_seconds: float = 60.0, after_delete: Callable[[], Any] | None = None) -> None:
        self.store, self.imap_factory, self.telegram = store, imap_factory, telegram
        self.worker_id, self.lease_seconds = worker_id or f"delete-{uuid.uuid4().hex}", lease_seconds
        self.after_delete = after_delete

    async def run_once(self, operation_id: str | None = None) -> DeleteOperation | None:
        if operation_id is not None:
            operation = await _call(self.store, ("claim_delete", "get_delete"), operation_id, self.worker_id, self.lease_seconds)
        else:
            operation = await _call(self.store, ("claim_next_delete",), self.worker_id, self.lease_seconds)
        if operation is None or operation.state == "tombstoned":
            return operation
        mappings = operation.provider_mapping
        if isinstance(mappings, dict):
            mappings = [mappings]
        # Newer adapters expose explicit phase flags.  The state string remains
        # for compatibility with older stores and is only used as a fallback.
        topic_deleted = bool(getattr(operation, "topic_deleted", False))
        provider_deleted = bool(getattr(operation, "provider_deleted", False))
        if not hasattr(operation, "topic_deleted"):
            topic_deleted = operation.state in {"telegram_deleted", "tombstoned"}
        if not hasattr(operation, "provider_deleted"):
            provider_deleted = operation.state in {"provider_deleted", "telegram_deleted", "tombstoned"}
        lease_token = getattr(operation, "lease_token", None)
        try:
            # Remove the Telegram surface first.  The UI also attempts this
            # optimistically, so a worker retry may arrive after the Topic is
            # already gone; the durable flag makes that retry idempotent.
            if not topic_deleted:
                if operation.telegram_topic_id is not None:
                    try:
                        result = await _delete_topic(self.telegram, operation.telegram_topic_id)
                    except Exception as exc:
                        # The UI records that it intentionally requested this
                        # exact Topic before making the external call.  A
                        # crash between Telegram's success response and the
                        # durable phase write therefore becomes recoverable:
                        # Telegram's definitive "missing" response is the
                        # idempotent success acknowledgement for this phase.
                        if (
                            bool(getattr(operation, "topic_delete_requested", False))
                            and (
                                bool(getattr(exc, "topic_missing", False))
                                or is_missing_forum_topic_error(exc)
                            )
                        ):
                            result = True
                        else:
                            raise
                    if result is not True:
                        raise RuntimeError("Telegram topic deletion was not confirmed")
                phase_result = await _call(
                    self.store, ("update_delete", "complete_delete"), operation.id,
                    "telegram_deleted", lease_token=lease_token,
                )
                if phase_result is False:
                    raise RuntimeError("delete lease expired before Topic phase commit")
                operation.state = "telegram_deleted"
                operation.topic_deleted = True
                topic_deleted = True

            if not provider_deleted:
                if not mappings:
                    if operation.thread_id is None and getattr(operation, "email_id", None) is None:
                        await _call(
                            self.store, ("update_delete", "complete_delete"), operation.id,
                            "failed", error="provider mapping is missing", lease_token=lease_token,
                        )
                        operation.state, operation.error = "failed", "provider mapping is missing"
                        return operation
                    # A thread may contain only local outgoing history (or a
                    # threadless outgoing message). There is no provider UID to
                    # delete, so the provider phase is already complete.
                    phase_result = await _call(
                        self.store, ("update_delete", "complete_delete"), operation.id,
                        "provider_deleted", lease_token=lease_token,
                    )
                    if phase_result is False:
                        raise RuntimeError("delete lease expired before provider phase commit")
                    operation.state = "provider_deleted"
                    operation.provider_deleted = True
                    provider_deleted = True
                else:
                    provider = self.imap_factory(operation) if callable(self.imap_factory) else self.imap_factory
                    pending = await _pending_delete_mappings(self.store, operation.id, mappings)
                    for mapping in pending:
                        try:
                            result = await _call(provider, ("delete", "delete_message", "delete_uid"), mapping)
                            if result is False:
                                raise RuntimeError("provider deletion was not confirmed")
                        except Exception as exc:
                            await _complete_delete_mapping(
                                self.store, operation.id, mapping, success=False,
                                error=str(exc), lease_token=lease_token,
                            )
                            raise
                        completed = await _complete_delete_mapping(
                            self.store, operation.id, mapping, success=True,
                            lease_token=lease_token,
                        )
                        if completed is False:
                            raise RuntimeError("delete lease expired before provider target commit")
                    phase_result = await _call(
                        self.store, ("update_delete", "complete_delete"), operation.id,
                        "provider_deleted", lease_token=lease_token,
                    )
                    if phase_result is False:
                        raise RuntimeError("delete lease expired before provider phase commit")
                    operation.state = "provider_deleted"
                    operation.provider_deleted = True
                    provider_deleted = True

            if topic_deleted and provider_deleted:
                if operation.thread_id is not None and hasattr(self.store, "tombstone_thread"):
                    tombstoned = await _call(
                        self.store, ("tombstone_thread",), operation.thread_id, operation.id,
                        lease_token=lease_token,
                    )
                    if not tombstoned:
                        raise RuntimeError("local thread tombstone was not confirmed")
                elif operation.thread_id is None:
                    email_id = getattr(operation, "email_id", None)
                    if email_id is None:
                        for mapping in mappings or ():
                            if mapping.get("email_id") is not None:
                                email_id = mapping["email_id"]
                                break
                    tombstone = getattr(self.store, "tombstone_email", None)
                    if email_id is not None and callable(tombstone):
                        try:
                            confirmed = await _maybe_await(tombstone(email_id, operation.id, lease_token=lease_token))
                        except TypeError:
                            confirmed = await _maybe_await(tombstone(email_id))
                        if not confirmed:
                            raise RuntimeError("local email tombstone was not confirmed")
                    else:
                        phase_result = await _call(
                            self.store, ("update_delete", "complete_delete"), operation.id,
                            "tombstoned", lease_token=lease_token,
                        )
                        if phase_result is False:
                            raise RuntimeError("delete lease expired before tombstone commit")
                else:
                    phase_result = await _call(
                        self.store, ("update_delete", "complete_delete"), operation.id,
                        "tombstoned", lease_token=lease_token,
                    )
                    if phase_result is False:
                        raise RuntimeError("delete lease expired before tombstone commit")
                operation.state, operation.error = "tombstoned", None
                operation.tombstoned = True
                if self.after_delete is not None:
                    try:
                        refreshed = self.after_delete()
                        if inspect.isawaitable(refreshed):
                            await refreshed
                    except Exception:
                        # Deletion is already durable; an Inbox refresh is only
                        # a convenience and must not make the saga retry.
                        pass
        except Exception as exc:
            # Keep the last completed stage; next run resumes after it.
            await _call(
                self.store, ("update_delete_error", "set_delete_error", "update_delete"),
                operation.id, operation.state, error=str(exc), lease_token=lease_token,
            )
            operation.error = str(exc)
        return operation


class MailProjectionWorker:
    """Replay durable Telegram projections once an admin private chat is bound."""
    def __init__(self, store: Any, telegram: Any, imap_factory: Any = None, *, worker_id: str | None = None, lease_seconds: float = 60.0) -> None:
        self.store, self.telegram, self.imap_factory = store, telegram, imap_factory
        self.worker_id, self.lease_seconds = worker_id or f"projection-{uuid.uuid4().hex}", lease_seconds

    async def run_once(self, *, include_failed: bool = True) -> ProjectionJob | None:
        job = await _call(
            self.store,
            ("claim_next_projection",),
            self.worker_id,
            self.lease_seconds,
            include_failed=include_failed,
        )
        if job is None:
            return None
        created_topic: Any = None
        assigned: Any = None
        delivery_topic: Any = None
        assignment_made = False
        try:
            if not has_projectable_mail_content(
                body_text=job.mail.text_body,
                summary=job.mail.summary,
                html_body=job.mail.html_body,
                attachments=job.mail.attachments,
            ):
                # Persist the intentional skip; a header-only message must not
                # repeatedly claim the queue or create an empty forum topic.
                await _complete_projection(self.store, job, True)
                return job
            thread = await _call(self.store, ("resolve_thread", "find_thread"), job.mail)
            if thread is None:
                created_topic = await _call(
                    self.telegram,
                    ("ensure_private_topic", "ensure_topic", "create_topic"),
                    job.account,
                    format_topic_name(job.mail.sender, job.mail.subject),
                )
                thread = created_topic
            assigned = await _call(self.store, ("assign_thread", "set_email_thread"), job.mail, thread)
            assignment_made = True
            delivery_topic = assigned or thread
            if created_topic is not None and not _same_topic(delivery_topic, created_topic):
                # A concurrent projection won the durable mapping. The topic just
                # created by this attempt has no messages, so remove it first.
                await _delete_new_topic(self.telegram, created_topic)
                created_topic = None
            result = await _call(self.telegram, ("project_mail", "send_mail", "send_to_topic"), job.mail, delivery_topic)
            if result is False:
                raise RuntimeError("Telegram projection rejected delivery")
            message_id = None
            message_kind = None
            if isinstance(result, list) and result and isinstance(result[0], dict):
                message_id = result[0].get("message_id")
                message_kind = result[0].get("message_kind")
            # Telegram delivery is the user-visible receipt boundary. Mark the
            # corresponding provider UID as read only after that call succeeds;
            # a flagging failure must not replay an already delivered Telegram
            # card on the next projection attempt.
            await self._mark_mail_read(job)
            await _complete_projection(
                self.store, job, True, message_id, message_kind=message_kind
            )
        except ProjectionWaitingBinding:
            logger.warning("Telegram projection is waiting for a private-chat binding: projection_id=%s", job.id)
            cleaned = await _compensate_empty_topic(self.store, self.telegram, job.mail, created_topic, assignment_made)
            if cleaned:
                await _complete_projection(self.store, job, False)
            else:
                await _mark_projection_ambiguous(self.store, job)
        except ProjectionDeliveryError as exc:
            logger.exception(
                "Telegram projection delivery failed: projection_id=%s sent_any=%s ambiguous=%s",
                job.id,
                exc.sent_any,
                exc.ambiguous,
            )
            if (
                exc.topic_missing
                and not exc.sent_any
                and created_topic is None
                and isinstance(delivery_topic, TelegramTopic)
            ):
                try:
                    if await self._replace_missing_topic(job, delivery_topic):
                        return job
                except Exception:
                    logger.exception(
                        "Telegram Topic replacement failed: projection_id=%s", job.id
                    )
            if exc.ambiguous or exc.sent_any:
                # A partial multi-fragment projection has already produced an
                # observable Telegram side effect, so retrying the whole card
                # would duplicate its first fragments.
                await _mark_projection_ambiguous(self.store, job)
            else:
                cleaned = await _compensate_empty_topic(self.store, self.telegram, job.mail, created_topic, assignment_made)
                if cleaned:
                    await _complete_projection(self.store, job, False)
                else:
                    await _mark_projection_ambiguous(self.store, job)
        except Exception as exc:
            logger.exception("Telegram projection failed: projection_id=%s", job.id)
            if bool(getattr(exc, "ambiguous", False)):
                await _mark_projection_ambiguous(self.store, job)
            else:
                cleaned = await _compensate_empty_topic(self.store, self.telegram, job.mail, created_topic, assignment_made)
                if cleaned:
                    await _complete_projection(self.store, job, False)
                else:
                    await _mark_projection_ambiguous(self.store, job)
        return job

    async def _mark_mail_read(self, job: ProjectionJob) -> None:
        if self.imap_factory is None:
            return
        try:
            provider = self.imap_factory(job.account) if callable(self.imap_factory) else self.imap_factory
            result = await _call(
                provider,
                ("mark_read", "mark_seen", "mark_message_read"),
                job.mail.mailbox,
                job.mail.uid,
                uidvalidity=job.mail.uidvalidity,
            )
            if result is False:
                raise RuntimeError("IMAP provider rejected read flag")
        except Exception:
            # The Telegram side effect is already confirmed. Keep the projection
            # terminal so any future provider-flag retry can happen independently
            # without duplicating the Telegram card.
            logger.warning(
                "unable to mark provider mail as read after Telegram delivery: account=%s mailbox=%s uid=%s",
                job.mail.account_id,
                job.mail.mailbox,
                job.mail.uid,
                exc_info=True,
            )

    async def _replace_missing_topic(
        self, job: ProjectionJob, stale_topic: TelegramTopic
    ) -> bool:
        """Replace one definitively missing Topic and requeue its whole thread."""

        if stale_topic.db_thread_id is None:
            return False
        replacement = await _call(
            self.telegram,
            ("ensure_private_topic", "ensure_topic", "create_topic"),
            job.account,
            format_topic_name(job.mail.sender, job.mail.subject),
        )
        if not isinstance(replacement, TelegramTopic):
            return False
        try:
            current = await _call(
                self.store,
                ("replace_deleted_topic",),
                int(stale_topic.db_thread_id),
                expected_chat_id=int(stale_topic.chat_id),
                expected_message_thread_id=int(stale_topic.message_thread_id),
                new_chat_id=int(replacement.chat_id),
                new_message_thread_id=int(replacement.message_thread_id),
            )
        except Exception:
            await _delete_new_topic(self.telegram, replacement)
            raise
        if not current or not bool(current.get("topic_replaced")):
            await _delete_new_topic(self.telegram, replacement)
        if not current:
            return False
        return (
            int(current.get("telegram_chat_id") or 0),
            int(current.get("telegram_message_thread_id") or 0),
        ) != (int(stale_topic.chat_id), int(stale_topic.message_thread_id))


OutboxWorker = MailOutboxWorker
IngestionWorker = MailIngestionWorker
DeleteWorker = MailDeleteWorker
ProjectionWorker = MailProjectionWorker


async def _max_ingested_uid(store: Any, account_id: Any, mailbox: str) -> int:
    method = getattr(store, "max_ingested_uid", None)
    if method is None:
        return 0
    value = method(account_id, mailbox)
    if inspect.isawaitable(value):
        value = await value
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


async def _imap_cursor(store: Any, account_id: Any, mailbox: str) -> dict[str, Any]:
    method = getattr(store, "get_imap_cursor", None)
    if method is None:
        return {"uidvalidity": None, "last_uid": await _max_ingested_uid(store, account_id, mailbox)}
    result = method(account_id, mailbox)
    if inspect.isawaitable(result):
        result = await result
    return dict(result or {"uidvalidity": None, "last_uid": 0})


async def _fetch_incremental(client: Any, mailbox: str, after_uid: int) -> FetchedMessages:
    result = await _call(client, ("fetch_incremental", "fetch_messages", "fetch_unread", "list_messages"), mailbox, after_uid=after_uid)
    if isinstance(result, FetchedMessages):
        return result
    return FetchedMessages(tuple(result or ()), None)


async def _advance_uid(store: Any, account_id: Any, mailbox: str, uidvalidity: str | None, uid: str) -> None:
    if not uidvalidity:
        return
    try:
        numeric_uid = int(uid)
    except (TypeError, ValueError):
        return
    result = await _call(store, ("advance_imap_cursor",), account_id, mailbox, uidvalidity, numeric_uid)
    if isinstance(result, dict) and result.get("reset_required"):
        await _call(store, ("reset_imap_cursor",), account_id, mailbox, uidvalidity, 0)


async def _pending_delete_mappings(store: Any, operation_id: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    method = getattr(store, "list_pending_delete_mappings", None)
    if method is None:
        return fallback
    value = method(operation_id)
    if inspect.isawaitable(value):
        value = await value
    return list(value or ())


async def _complete_delete_mapping(store: Any, operation_id: Any, mapping: dict[str, Any], *, success: bool,
                                   error: str | None = None, lease_token: str | None = None) -> Any:
    method = getattr(store, "complete_delete_mapping", None)
    if method is None:
        return
    try:
        value = method(operation_id, mapping, success=success, error=error, lease_token=lease_token)
    except TypeError:
        # Keep small legacy fakes source-compatible while the v2 adapter uses
        # the lease token for a compare-and-set target update.
        value = method(operation_id, mapping, success=success, error=error)
    if inspect.isawaitable(value):
        await value


async def _complete_projection(store: Any, job: ProjectionJob, success: bool,
                               telegram_message_id: int | None = None,
                               *, message_kind: str | None = None) -> None:
    if job.lease_token:
        await _call(
            store,
            ("complete_projection",),
            job.id,
            success,
            telegram_message_id,
            lease_token=job.lease_token,
            message_kind=message_kind,
        )
    else:
        await _call(
            store,
            ("complete_projection",),
            job.id,
            success,
            telegram_message_id,
            message_kind=message_kind,
        )


async def _mark_projection_ambiguous(store: Any, job: ProjectionJob) -> None:
    """Quarantine an uncertain Bot API delivery; it must be reconciled manually."""

    method = getattr(store, "mark_projection_ambiguous", None)
    if not callable(method):
        # A real durable store will move a lease-expired row to ambiguous. Do not
        # mark it failed here, because that would authorize an unsafe resend.
        return
    try:
        value = method(job.id, lease_token=job.lease_token)
    except TypeError:
        value = method(job.id)
    if inspect.isawaitable(value):
        await value


def _same_topic(left: Any, right: Any) -> bool:
    if left is right or left == right:
        return True
    if isinstance(left, TelegramTopic) and isinstance(right, TelegramTopic):
        return (left.chat_id, left.message_thread_id) == (right.chat_id, right.message_thread_id)
    return False


async def _delete_new_topic(telegram: Any, topic: Any) -> bool:
    try:
        result = await _delete_topic(telegram, topic)
        return result is True
    except Exception:
        return False


async def _delete_topic(telegram: Any, topic: Any) -> Any:
    """Delete a Topic through either the projection adapter or raw Bot client."""
    delete_topic = getattr(telegram, "delete_topic", None)
    if callable(delete_topic):
        return await _maybe_await(delete_topic(topic))

    delete_forum_topic = getattr(telegram, "delete_forum_topic", None)
    if not callable(delete_forum_topic):
        raise AttributeError("telegram client does not support Topic deletion")
    if isinstance(topic, TelegramTopic):
        return await _maybe_await(
            delete_forum_topic(topic.chat_id, topic.message_thread_id)
        )
    if isinstance(topic, dict):
        chat_id = topic.get("chat_id", topic.get("telegram_chat_id"))
        thread_id = topic.get("message_thread_id", topic.get("telegram_message_thread_id"))
        if chat_id is not None and thread_id is not None:
            return await _maybe_await(delete_forum_topic(chat_id, int(thread_id)))
    return await _maybe_await(delete_forum_topic(topic))


async def _compensate_empty_topic(store: Any, telegram: Any, mail: IncomingMail, topic: Any, assignment_made: bool) -> bool:
    """Remove only a topic created by this failed attempt and never an existing one.

    Keep the mapping until Telegram confirms deletion. If cleanup itself fails,
    the caller quarantines the projection as ambiguous so a retry cannot keep
    creating fresh topics for the same mail.
    """

    if topic is None:
        return True
    if not await _delete_new_topic(telegram, topic):
        return False
    if assignment_made:
        rollback = getattr(store, "rollback_new_topic_assignment", None) or getattr(store, "rollback_thread_assignment", None)
        if not callable(rollback):
            return False
        try:
            value = rollback(mail, topic)
            if inspect.isawaitable(value):
                value = await value
            if value is False:
                return False
        except Exception:
            return False
    return True
