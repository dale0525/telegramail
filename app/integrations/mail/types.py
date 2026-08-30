"""Transport-neutral mail value objects used by the v2 pipeline.

The v2 code deliberately contains no Telegram/TDLib imports.  Its callers can
provide a small repository and a small Telegram projection client instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


SendState = Literal["queued", "sending", "sent", "failed", "ambiguous"]
SummaryState = Literal["pending", "running", "completed", "succeeded", "skipped", "failed"]


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    mime_type: str = "application/octet-stream"
    # Incoming ``multipart/related`` mail commonly embeds logos and receipts as
    # ``cid:...`` images. Keep the identifier with the bytes so the durable
    # store can later serve the resource only to the authenticated Mini App.
    content_id: str | None = None
    is_inline: bool = False


@dataclass(frozen=True)
class MailDraft:
    operation_id: str
    account_id: int | str
    from_email: str
    subject: str
    text_body: str = ""
    markdown_body: str | None = None
    html_body: str | None = None
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    from_name: str | None = None
    reply_to: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    message_id: str | None = None
    # Set when an API has persisted the editable draft in app.db before queueing.
    draft_id: int | None = None
    # Optional persisted thread used to refresh the latest-message projection
    # after an outgoing SMTP delivery.
    thread_id: int | None = None
    # Ephemeral connection settings supplied by a repository adapter.  Passwords
    # are decrypted only in process memory and are never persisted in this value.
    transport_account: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class IncomingMail:
    account_id: int | str
    mailbox: str
    uid: str
    # IMAP UID values are only unique inside one UIDVALIDITY epoch.
    uidvalidity: str | None = None
    message_id: str | None = None
    sender: str = ""
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    subject: str = ""
    text_body: str = ""
    html_body: str | None = None
    received_at: str = ""
    summary: str | None = None
    category: str | None = None
    priority: str | None = None
    # Canonical, sanitized links selected by the LLM.  Keep this as an
    # in-process value only; durable adapters may persist a JSON projection
    # separately without coupling this transport object to SQLite.
    important_links: list[dict[str, str]] = field(default_factory=list)
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    raw: Any = None
    # Local database id, when a repository adapter can provide one.  IMAP
    # transports leave it unset; the summary worker may use it for id-based
    # updates after a process restart.
    email_id: int | None = None


@dataclass(frozen=True)
class FetchedMessages:
    """One mailbox UID-range fetch plus the server identity used for its cursor."""
    messages: tuple[IncomingMail, ...]
    uidvalidity: str | None = None


@dataclass
class SendOperation:
    id: str
    draft: MailDraft
    state: SendState = "queued"
    lease_token: str | None = None
    lease_until: float | None = None
    attempts: int = 0
    error: str | None = None
    sent_at: float | None = None
    reconciled_at: float | None = None


@dataclass
class DeleteOperation:
    id: str
    account_id: int | str
    mailbox: str
    uid: str
    provider_mapping: Any = None
    thread_id: int | str | None = None
    telegram_topic_id: int | str | None = None
    telegram_chat_id: int | str | None = None
    # Durable target id for a threadless local tombstone.  Kept optional for
    # compatibility with older operation payloads that only carried UID data.
    email_id: int | str | None = None
    state: str = "queued"
    error: str | None = None
    # Durable phase flags let the delete saga resume safely when Telegram is
    # removed before the mailbox provider finishes.  They are optional at the
    # type boundary so older in-memory callers can keep constructing the
    # operation with the historical fields only.
    provider_deleted: bool = False
    topic_delete_requested: bool = False
    topic_deleted: bool = False
    tombstoned: bool = False
    # The active worker lease is carried through every phase commit so an
    # expired worker cannot overwrite a newer worker's progress.
    lease_token: str | None = None
    lease_until: float | None = None
    attempts: int = 0


@dataclass(frozen=True)
class ProjectionJob:
    id: int | str
    mail: IncomingMail
    account: Any
    lease_token: str | None = None


@dataclass
class SummaryJob:
    """An asynchronous, retryable LLM analysis task for one persisted email."""

    id: int | str
    mail: IncomingMail
    state: SummaryState = "pending"
    attempts: int = 0
    lease_token: str | None = None
    lease_until: float | None = None
    error: str | None = None
    analysis: dict[str, Any] | None = None
