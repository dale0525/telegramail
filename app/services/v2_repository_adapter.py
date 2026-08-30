"""Duck-typed bridge from ``app.db.V2Repository`` to v2 mail workers.

The adapter is kept outside ``app.db`` so the workers remain testable against a
small protocol.  It also gives old and new repository calls one stable shape.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.email_utils.markdown_render import render_markdown_to_html

from app.integrations.mail.telegram import TelegramTopic
from app.integrations.mail.types import Attachment, DeleteOperation, IncomingMail, MailDraft, ProjectionJob, SendOperation


_INLINE_IMAGE_MIME_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/avif",
})
_MAX_INLINE_ASSET_BYTES = 10 * 1024 * 1024
_MAX_INLINE_ASSETS_PER_EMAIL = 32
_CONTENT_ID_RE = re.compile(r"^[^\x00-\x20\x7f<>]{1,512}$")


def _decode_row_links(row: Any) -> list[dict[str, str]]:
    """Decode the durable LLM link projection for worker value objects."""

    value: Any = None
    for key in ("important_links", "llm_important_links_json", "important_links_json"):
        try:
            value = row[key]
        except (KeyError, IndexError):
            continue
        if value is not None:
            break
    if value is None:
        return []
    try:
        from app.email_utils.llm import sanitize_important_links

        return sanitize_important_links(value)
    except Exception:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []


class V2MailRepositoryAdapter:
    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self._email_ids: dict[tuple[str, str, str, str], int] = {}

    @staticmethod
    def _email_key(mail: IncomingMail) -> tuple[str, str, str, str]:
        return (str(mail.account_id), mail.mailbox.casefold(), str(mail.uidvalidity or ""), str(mail.uid))

    def enqueue_send(self, operation: SendOperation) -> bool:
        # The HTTP/API layer creates a durable draft first.  For direct callers
        # without a draft_id, retain the operation only when the repository offers
        # the native object API (e.g. the in-memory test implementation).
        draft_id = getattr(operation.draft, "draft_id", None)
        if draft_id is None:
            raise ValueError("a persisted v2 draft_id is required before queueing send")
        self.repository.enqueue_send(int(operation.draft.account_id), str(operation.id), draft_id=int(draft_id))
        return True

    def get_send(self, operation_id: Any) -> SendOperation | None:
        row = self.repository.get_send(int(operation_id))
        return self._send(row) if row else None

    def claim_send(self, operation_id: Any, worker_id: str, lease_seconds: float, now: float | None = None) -> SendOperation | None:
        row = self.repository.claim_send(int(operation_id), lease_token=worker_id, lease_seconds=int(lease_seconds))
        return self._send(row) if row else None

    def claim_next_send(self, worker_id: str, lease_seconds: float, now: float | None = None) -> SendOperation | None:
        row = self.repository.claim_next_send(lease_token=worker_id, lease_seconds=int(lease_seconds))
        return self._send(row) if row else None

    def complete_send(self, operation_id: Any, lease_token: str, state: str, *, error: str | None = None,
                      provider_message_id: str | None = None, now: float | None = None) -> bool:
        if state == "ambiguous":
            result = self.repository.set_send_state(int(operation_id), "ambiguous", expected_statuses=("sending",), error_code="timeout", error_message=error)
        else:
            result = self.repository.complete_send(
                int(operation_id), lease_token=lease_token, success=state == "sent",
                error_code="smtp_error" if state == "failed" else None,
                error_message=error, provider_message_id=provider_message_id,
            )
        return result is not None

    def complete_send_with_outgoing(self, operation_id: Any, lease_token: str, *,
                                    provider_message_id: str | None, draft: MailDraft) -> bool:
        """Finalize a successful send and its outgoing history atomically."""
        finalizer = getattr(self.repository, "complete_send_with_outgoing", None)
        if not callable(finalizer):
            # Compatibility fallback for older repositories.  The current v2
            # repository implements the transaction above; this branch keeps
            # custom test stores source-compatible during rolling upgrades.
            result = self.complete_send(operation_id, lease_token, "sent", provider_message_id=provider_message_id)
            if not result:
                return False
            self.record_outgoing_email(draft)
            return True
        result = finalizer(
            int(operation_id), lease_token=lease_token,
            provider_message_id=provider_message_id,
            outgoing={
                "account_id": int(draft.account_id), "thread_id": draft.thread_id,
                "message_id": draft.message_id or provider_message_id,
                "sender": draft.from_email, "recipient": ", ".join(draft.to),
                "cc": ", ".join(draft.cc), "bcc": ", ".join(draft.bcc),
                "subject": draft.subject,
                "body_text": draft.text_body or draft.markdown_body or "",
                "body_html": draft.html_body, "in_reply_to": draft.in_reply_to,
                "references_header": " ".join(draft.references),
            },
        )
        return result is not None

    def record_outgoing_email(self, draft: MailDraft) -> dict[str, Any] | None:
        """Persist a successful SMTP delivery for thread projection purposes."""

        recorder = getattr(self.repository, "insert_outgoing_email", None)
        if not callable(recorder):
            return None
        return recorder(
            int(draft.account_id), thread_id=draft.thread_id, message_id=draft.message_id,
            sender=draft.from_email, recipient=", ".join(draft.to), cc=", ".join(draft.cc),
            bcc=", ".join(draft.bcc), subject=draft.subject,
            body_text=draft.text_body or draft.markdown_body or "", body_html=draft.html_body,
            in_reply_to=draft.in_reply_to, references_header=" ".join(draft.references),
        )

    def update_account_connection_status(self, account_id: Any, *, ok: bool, error: str | None = None) -> bool:
        return self.repository.update_account_connection_status(int(account_id), ok=bool(ok), error=error) is not None

    def mark_reconciled_sent(self, operation_id: Any, *, provider_message_id: str | None = None) -> bool:
        return self.repository.mark_reconciled_sent(int(operation_id), provider_message_id=provider_message_id) is not None

    def mark_reconciled_sent_with_outgoing(self, operation_id: Any, *, provider_message_id: str | None,
                                           draft: MailDraft) -> bool:
        finalizer = getattr(self.repository, "mark_reconciled_sent_with_outgoing", None)
        if not callable(finalizer):
            if not self.mark_reconciled_sent(operation_id, provider_message_id=provider_message_id):
                return False
            self.record_outgoing_email(draft)
            return True
        result = finalizer(
            int(operation_id), provider_message_id=provider_message_id,
            outgoing={
                "account_id": int(draft.account_id), "thread_id": draft.thread_id,
                "message_id": draft.message_id or provider_message_id,
                "sender": draft.from_email, "recipient": ", ".join(draft.to),
                "cc": ", ".join(draft.cc), "bcc": ", ".join(draft.bcc),
                "subject": draft.subject,
                "body_text": draft.text_body or draft.markdown_body or "",
                "body_html": draft.html_body, "in_reply_to": draft.in_reply_to,
                "references_header": " ".join(draft.references),
            },
        )
        return result is not None

    def set_send_state(self, operation_id: Any, state: str, *, expected_statuses: tuple[str, ...] | None = None,
                       error: str | None = None) -> bool:
        kwargs: dict[str, Any] = {}
        if expected_statuses is not None:
            kwargs["expected_statuses"] = expected_statuses
        if error is not None:
            kwargs.update(error_code="send_finalize", error_message=error)
        return self.repository.set_send_state(int(operation_id), state, **kwargs) is not None

    def acquire_ingestion_lease(self, account_id: Any, mailbox: str, owner_id: str, lease_seconds: float, now: float | None = None) -> bool:
        return bool(self.repository.acquire_ingestion_lease(int(account_id), owner_id, ttl_seconds=int(lease_seconds)))

    def release_ingestion_lease(self, account_id: Any, mailbox: str, owner_id: str) -> bool:
        return bool(self.repository.release_ingestion_lease(int(account_id), owner_id))

    def max_ingested_uid(self, account_id: Any, mailbox: str) -> int:
        """Return the durable numeric UID cursor for one account/mailbox."""
        value = self.repository.get_max_uid(int(account_id), mailbox)
        return int(value or 0)

    def get_imap_cursor(self, account_id: Any, mailbox: str) -> dict[str, Any]:
        return dict(self.repository.get_or_bootstrap_imap_cursor(int(account_id), mailbox))

    def reset_imap_cursor(self, account_id: Any, mailbox: str, uidvalidity: str, last_uid: int = 0) -> dict[str, Any]:
        return dict(self.repository.reset_imap_cursor(int(account_id), mailbox, uidvalidity, last_uid=int(last_uid)))

    def advance_imap_cursor(self, account_id: Any, mailbox: str, uidvalidity: str, last_uid: int) -> dict[str, Any]:
        return dict(self.repository.advance_imap_cursor(int(account_id), mailbox, uidvalidity, int(last_uid)))

    def insert_incoming_if_absent(self, mail: IncomingMail) -> bool:
        key = self._email_key(mail)
        row = self.repository.insert_incoming_if_absent(int(mail.account_id), mailbox=mail.mailbox, uid=mail.uid,
            uidvalidity=mail.uidvalidity,
            message_id=mail.message_id, sender=mail.sender, recipient=", ".join(mail.to), cc=", ".join(mail.cc), subject=mail.subject, email_date=mail.received_at,
            body_text=mail.text_body, body_html=mail.html_body, in_reply_to=mail.in_reply_to, references_header=" ".join(mail.references),
            important_links=getattr(mail, "important_links", ()))
        email_id = int(row["id"])
        self._email_ids[key] = email_id
        # Inline resources must also be persisted when the message already
        # exists.  A previous process may have stored the HTML first and then
        # crashed before writing its CID assets; retrying ingestion repairs it.
        self.persist_inline_assets(email_id, mail.attachments)
        self.persist_email_attachments(email_id, mail.attachments)
        return bool(row["is_new"])

    @staticmethod
    def _normalize_content_id(value: str | None) -> str | None:
        content_id = str(value or "").strip()
        if content_id.startswith("<") and content_id.endswith(">"):
            content_id = content_id[1:-1].strip()
        if not content_id or not _CONTENT_ID_RE.fullmatch(content_id):
            return None
        return content_id

    @staticmethod
    def _safe_inline_filename(value: str | None) -> str:
        filename = Path(str(value or "inline-image")).name
        filename = "".join(char for char in filename if ord(char) >= 0x20 and char not in {"/", "\\", "\x7f"})
        return (filename[:255] or "inline-image")

    def persist_inline_assets(self, email_id: int, attachments: Any) -> int:
        """Write safe image CID parts and register their authenticated metadata.

        The bytes are addressed by SHA-256 rather than a provider filename.  A
        temporary file in the destination directory plus ``os.replace`` keeps a
        concurrent reader from seeing a partial image.
        """
        upsert = getattr(self.repository, "upsert_email_inline_asset", None)
        if not callable(upsert):
            return 0
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        inline_root = (data_root / "inline-assets").resolve()
        asset_root = (inline_root / str(int(email_id))).resolve()
        if not asset_root.is_relative_to(inline_root):
            raise ValueError("invalid inline asset root")
        persisted = 0
        inline_seen = 0
        for attachment in tuple(attachments or ()):
            if not isinstance(attachment, Attachment) or not attachment.is_inline:
                continue
            inline_seen += 1
            if inline_seen > _MAX_INLINE_ASSETS_PER_EMAIL:
                break
            content_id = self._normalize_content_id(attachment.content_id)
            mime_type = str(attachment.mime_type or "").split(";", 1)[0].strip().lower()
            data = bytes(attachment.data or b"")
            if content_id is None or mime_type not in _INLINE_IMAGE_MIME_TYPES or not data:
                continue
            if len(data) > _MAX_INLINE_ASSET_BYTES:
                continue
            digest = hashlib.sha256(data).hexdigest()
            asset_root.mkdir(parents=True, exist_ok=True)
            destination = (asset_root / f"{digest}.bin").resolve()
            if not destination.is_relative_to(asset_root):
                raise ValueError("invalid inline asset path")
            if not destination.is_file() or destination.stat().st_size != len(data):
                temporary_name: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=asset_root, prefix=f".{digest}.", suffix=".tmp", delete=False
                    ) as temporary:
                        temporary_name = temporary.name
                        temporary.write(data)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    os.replace(temporary_name, destination)
                finally:
                    if temporary_name:
                        try:
                            Path(temporary_name).unlink(missing_ok=True)
                        except OSError:
                            pass
            row = upsert(
                int(email_id), content_id=content_id,
                file_name=self._safe_inline_filename(attachment.filename), mime_type=mime_type,
                size=len(data), local_path=str(destination.relative_to(data_root)), sha256=digest,
            )
            # ``upsert`` intentionally returns the durable row.  Keep old files
            # for now: a repeated CID update is rare and cleanup is safe to do in
            # account purge, while avoiding a race with an in-flight response.
            if row:
                persisted += 1
        return persisted

    def persist_email_attachments(self, email_id: int, attachments: Any) -> int:
        """Persist non-inline incoming MIME parts under ``data/`` safely.

        The mail parser hands us immutable bytes.  Write each part atomically
        below an email-specific directory, then register only a relative path
        in SQLite.  Replaying the same UID is therefore idempotent while an
        account/thread purge can remove files by a narrowly-scoped root.
        """

        upsert = getattr(self.repository, "upsert_email_attachment", None)
        if not callable(upsert):
            return 0
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        attachment_root = (data_root / "email-attachments" / str(int(email_id))).resolve()
        parent_root = (data_root / "email-attachments").resolve()
        if not attachment_root.is_relative_to(parent_root):
            raise ValueError("invalid email attachment root")
        persisted = 0
        part_index = 0
        for attachment in tuple(attachments or ()):
            if not isinstance(attachment, Attachment) or bool(attachment.is_inline):
                continue
            data = bytes(attachment.data or b"")
            digest = hashlib.sha256(data).hexdigest()
            # Leave room for the stable position/hash prefix under common
            # 255-byte filesystem component limits (measured in encoded bytes).
            safe_name = self._safe_inline_filename(attachment.filename or "attachment")
            while len(safe_name.encode("utf-8")) > 180:
                safe_name = safe_name[:-1]
            safe_name = safe_name or "attachment"
            destination = (attachment_root / f"{part_index}-{digest[:24]}-{safe_name}").resolve()
            if not destination.is_relative_to(attachment_root):
                raise ValueError("invalid email attachment path")
            attachment_root.mkdir(parents=True, exist_ok=True)
            if not destination.is_file() or destination.stat().st_size != len(data):
                temporary_name: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=attachment_root, prefix=f".{part_index}-{digest[:8]}.", suffix=".tmp", delete=False
                    ) as temporary:
                        temporary_name = temporary.name
                        temporary.write(data)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    os.replace(temporary_name, destination)
                finally:
                    if temporary_name:
                        try:
                            Path(temporary_name).unlink(missing_ok=True)
                        except OSError:
                            pass
            row = upsert(
                int(email_id), part_index=part_index,
                file_name=safe_name,
                mime_type=str(attachment.mime_type or "application/octet-stream"),
                size=len(data),
                local_path=str(destination.relative_to(data_root)),
                sha256=digest,
            )
            if row:
                persisted += 1
            part_index += 1
        return persisted

    def _load_email_attachments(self, email_id: int) -> tuple[Attachment, ...]:
        """Hydrate validated non-inline MIME bytes for Telegram delivery."""

        lister = getattr(self.repository, "list_email_attachments", None)
        if not callable(lister):
            return ()
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        attachment_root = (data_root / "email-attachments" / str(int(email_id))).resolve()
        result: list[Attachment] = []
        for row in lister(int(email_id)) or ():
            local_path = row.get("local_path")
            if not local_path:
                continue
            path = (data_root / str(local_path)).resolve()
            if not path.is_relative_to(attachment_root) or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            digest = row.get("sha256")
            if digest and hashlib.sha256(data).hexdigest() != str(digest):
                continue
            result.append(Attachment(
                str(row.get("file_name") or path.name), data,
                str(row.get("mime_type") or "application/octet-stream"),
            ))
        return tuple(result)

    def upsert_contact(self, account_id: Any, address: str) -> None:
        self.repository.upsert_contact(int(account_id), address)

    def update_incoming_labels(self, mail: IncomingMail, analysis: dict[str, Any]) -> bool:
        email_id = getattr(mail, "email_id", None) or self._email_ids.get(self._email_key(mail))
        if email_id is None:
            return False
        return self.repository.update_email_llm_labels(email_id=email_id,
                                                        category=str(analysis.get("category") or "other"),
                                                        priority=str(analysis.get("priority") or "medium"),
                                                        confidence=analysis.get("category_confidence"),
                                                        summary=analysis.get("summary"),
                                                        important_links=analysis.get(
                                                            "important_links", analysis.get("urls", ())
                                                        )) is not None

    # Durable LLM summary queue -----------------------------------------
    #
    # The worker deliberately talks to a small duck-typed protocol.  Keep the
    # repository's email-id based queue behind this adapter so an IMAP receipt
    # can enqueue work without making the ingestion path know about SQLite.
    def get_global_llm_settings(self) -> dict[str, Any]:
        getter = getattr(self.repository, "get_global_llm_settings", None)
        if not callable(getter):
            getter = getattr(self.repository, "load_llm_settings", None)
        if not callable(getter):
            return {}
        value = getter()
        return dict(value or {})

    load_llm_settings = get_global_llm_settings

    def _email_id_for_mail(self, mail: IncomingMail) -> int:
        email_id = getattr(mail, "email_id", None) or self._email_ids.get(self._email_key(mail))
        if email_id is None:
            raise KeyError("incoming email has not been persisted")
        return int(email_id)

    def enqueue_summary(self, mail: IncomingMail) -> dict[str, Any]:
        email_id = self._email_id_for_mail(mail)
        task = self.repository.enqueue_summary_task(email_id)
        # Carry the durable email id on the value object.  It survives the
        # in-process handoff and lets persistence work after a worker restart.
        queued_mail = mail if getattr(mail, "email_id", None) == email_id else IncomingMail(
            **{**mail.__dict__, "email_id": email_id}
        )
        return {**dict(task or {}), "id": email_id, "email_id": email_id, "mail": queued_mail}

    enqueue_llm_summary = enqueue_summary

    def claim_next_summary(
        self,
        worker_id: str | None = None,
        lease_seconds: float = 300.0,
        *,
        include_failed: bool = True,
    ) -> dict[str, Any] | None:
        claim = self.repository.claim_summary_task(
            lease_seconds=max(1, int(lease_seconds)), include_failed=include_failed
        )
        if not claim:
            return None
        email_id = int(claim["email_id"])
        conn = self.repository.db.connect()
        try:
            row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
            account = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(row["account_id"]),)).fetchone() if row else None
        finally:
            conn.close()
        if row is None or account is None:
            # A deleted/corrupt row must not leave a leased task stuck forever.
            self.repository.complete_summary_task(
                email_id, success=False, status="failed", error="email_not_found",
                lease_token=claim.get("lease_token"),
            )
            return None
        mail = self._mail_from_row(row)
        self._email_ids[self._email_key(mail)] = email_id
        return {
            **dict(claim),
            # Worker completion is keyed by email id; task id is retained for
            # diagnostics without leaking it into the public API.
            "id": email_id,
            "email_id": email_id,
            "task_id": int(claim.get("id") or 0),
            "mail": mail,
            "account": dict(account),
        }

    claim_next_llm_summary = claim_next_summary

    def complete_summary(
        self,
        email_id: int | str,
        success: bool = True,
        *,
        analysis: dict[str, Any] | None = None,
        error: str | None = None,
        lease_token: str | None = None,
        status: str | None = None,
    ) -> bool:
        result = self.repository.complete_summary_task(
            int(email_id), success=bool(success), error=error, status=status,
            lease_token=lease_token,
        )
        return result is not None

    complete_llm_summary = complete_summary

    def _mail_from_row(self, row: Any) -> IncomingMail:
        email_id = int(row["id"])
        return IncomingMail(
            account_id=row["account_id"], mailbox=row["mailbox"], uid=str(row["uid"]),
            uidvalidity=row["uidvalidity"] or None, message_id=row["message_id"],
            sender=row["sender"] or "",
            to=tuple(filter(None, (row["recipient"] or "").split(","))),
            cc=tuple(filter(None, (row["cc"] or "").split(","))),
            subject=row["subject"] or "", text_body=row["body_text"] or "",
            html_body=row["body_html"], received_at=row["email_date"] or "",
            summary=row["llm_summary"], category=row["llm_category"],
            priority=row["llm_priority"], in_reply_to=row["in_reply_to"],
            references=tuple((row["references_header"] or "").split()),
            important_links=_decode_row_links(row),
            attachments=self._load_email_attachments(email_id),
            email_id=email_id,
        )

    def mark_projection_waiting(self, mail: IncomingMail) -> bool:
        """Record a deferred projection when the private chat is not bound.

        Older repositories lack this optional durable queue; returning false keeps
        the already-persisted message safe and avoids a wrong-chat projection.
        """
        email_id = self._email_ids.get(self._email_key(mail)) or getattr(mail, "email_id", None)
        if email_id is None:
            raise KeyError("incoming email has not been persisted")
        return bool(self.repository.mark_projection_waiting(email_id))

    def enqueue_projection(self, mail: IncomingMail) -> bool:
        return self.mark_projection_waiting(mail)

    def claim_next_projection(self, worker_id: str | None = None,
                              lease_seconds: float = 60.0,
                              include_failed: bool = True) -> ProjectionJob | None:
        part = self.repository.claim_next_projection(
            lease_token=worker_id,
            lease_seconds=int(lease_seconds),
            include_failed=include_failed,
        )
        if not part:
            return None
        conn = self.repository.db.connect()
        try:
            row = conn.execute("SELECT * FROM emails WHERE id = ?", (int(part["email_id"]),)).fetchone()
            account = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(row["account_id"]),)).fetchone() if row else None
        finally:
            conn.close()
        if row is None or account is None:
            self.repository.complete_projection(int(part["id"]), lease_token=str(part["lease_token"]), success=False)
            return None
        mail = self._mail_from_row(row)
        # Projection replay commonly happens after a process restart, when the
        # ingestion-time cache is empty. Restore the durable email identity so
        # assign_thread can bind the newly created Topic to this exact row.
        self._email_ids[self._email_key(mail)] = int(row["id"])
        return ProjectionJob(id=int(part["id"]), mail=mail, account=dict(account), lease_token=part.get("lease_token"))

    def replay_pending_projections(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.replay_pending_projections(limit=limit)

    def requeue_projection_after_reconciliation(self, projection_id: int | str) -> bool:
        """Explicitly permit a resend after an ambiguous Telegram delivery is checked."""
        return self.repository.requeue_projection_after_reconciliation(int(projection_id)) is not None

    def complete_projection(
        self,
        projection_id: int | str,
        success: bool,
        telegram_message_id: int | None = None,
        *,
        lease_token: str | None = None,
        message_kind: str | None = None,
    ) -> bool:
        if not lease_token:
            return False
        return self.repository.complete_projection(int(projection_id), lease_token=lease_token, success=success,
                                                   telegram_message_id=telegram_message_id,
                                                   message_kind=message_kind) is not None

    def mark_projection_ambiguous(self, projection_id: int | str, *, lease_token: str | None = None) -> bool:
        if not lease_token:
            return False
        return self.repository.mark_projection_ambiguous(int(projection_id), lease_token=lease_token) is not None

    def resolve_thread(self, mail: IncomingMail) -> TelegramTopic | None:
        row = self.repository.resolve_thread(int(mail.account_id), message_id=mail.message_id, in_reply_to=mail.in_reply_to,
                                           references_header=" ".join(mail.references), subject=mail.subject)
        if not row or row.get("telegram_chat_id") is None or row.get("telegram_message_thread_id") is None:
            return None
        return TelegramTopic(row["telegram_chat_id"], int(row["telegram_message_thread_id"]), int(row["id"]))

    def assign_thread(self, mail: IncomingMail, thread_id: Any) -> TelegramTopic:
        key = self._email_key(mail)
        email_id = self._email_ids.get(key)
        if email_id is None:
            raise KeyError("incoming email has not been persisted")
        if isinstance(thread_id, TelegramTopic):
            if thread_id.db_thread_id is not None:
                row = self.repository.assign_thread(email_id, thread_id.db_thread_id, account_id=int(mail.account_id), root_message_id=mail.message_id, subject=mail.subject)
            else:
                row = self.repository.assign_thread(email_id, None, account_id=int(mail.account_id), root_message_id=mail.message_id, subject=mail.subject,
                                                    telegram_chat_id=int(thread_id.chat_id), telegram_message_thread_id=thread_id.message_thread_id)
        else:
            row = self.repository.assign_thread(email_id, int(thread_id), account_id=int(mail.account_id), root_message_id=mail.message_id, subject=mail.subject)
        if not row or row.get("telegram_chat_id") is None or row.get("telegram_message_thread_id") is None:
            raise ValueError("thread projection mapping was not persisted")
        return TelegramTopic(row["telegram_chat_id"], int(row["telegram_message_thread_id"]), int(row["id"]))

    def replace_deleted_topic(self, thread_id: int, **mapping: Any) -> Dict[str, Any] | None:
        return self.repository.replace_deleted_topic(int(thread_id), **mapping)

    def rollback_new_topic_assignment(self, mail: IncomingMail, topic: TelegramTopic) -> bool:
        email_id = self._email_ids.get(self._email_key(mail))
        if email_id is None:
            return False
        return self.repository.rollback_new_topic_assignment(
            int(email_id), telegram_chat_id=int(topic.chat_id), telegram_message_thread_id=int(topic.message_thread_id)
        ) is not None

    def claim_delete(self, operation_id: Any, worker_id: str, lease_seconds: float, now: float | None = None) -> DeleteOperation | None:
        row = self.repository.claim_delete(int(operation_id), lease_token=worker_id, lease_seconds=int(lease_seconds))
        return self._delete(row) if row else None

    def claim_next_delete(self, worker_id: str, lease_seconds: float, now: float | None = None) -> DeleteOperation | None:
        row = self.repository.claim_next_delete(lease_token=worker_id, lease_seconds=int(lease_seconds))
        return self._delete(row) if row else None

    def get_delete(self, operation_id: Any, *_: Any) -> DeleteOperation | None:
        row = self.repository.get_delete(int(operation_id))
        return self._delete(row) if row else None

    def update_delete(self, operation_id: Any, state: str, *, error: str | None = None,
                      lease_token: str | None = None) -> bool:
        if error is not None:
            return self.repository.update_delete(
                int(operation_id), success=False, error_code="delete_error",
                error_message=error, lease_token=lease_token,
            ) is not None
        if state == "provider_deleted":
            return self.repository.update_delete(
                int(operation_id), status="deleting", provider_deleted=True,
                lease_token=lease_token,
            ) is not None
        if state == "telegram_deleted":
            return self.repository.update_delete(
                int(operation_id), status="deleting", topic_deleted=True,
                lease_token=lease_token,
            ) is not None
        if state == "tombstoned":
            return self.repository.update_delete(
                int(operation_id), success=True, provider_deleted=True,
                topic_deleted=True, tombstoned=True, lease_token=lease_token,
            ) is not None
        return self.repository.update_delete(
            int(operation_id), status=state, lease_token=lease_token,
        ) is not None

    def mark_delete_topic_deleted(self, operation_id: Any) -> bool:
        """Record an optimistic Topic removal initiated by the Telegram UI."""

        return self.repository.mark_delete_topic_deleted(int(operation_id)) is not None

    def mark_delete_topic_requested(self, operation_id: Any) -> bool:
        """Record that the UI intentionally requested Topic removal."""

        return self.repository.mark_delete_topic_requested(int(operation_id)) is not None

    def tombstone_thread(self, thread_id: int | str, operation_id: int | str,
                         *, lease_token: str | None = None) -> bool:
        return self.repository.tombstone_thread(
            int(thread_id), delete_operation_id=int(operation_id), lease_token=lease_token,
        ) is not None

    def tombstone_email(self, email_id: int | str, operation_id: int | str | None = None,
                        *, lease_token: str | None = None) -> bool:
        """Finalize a threadless delete after provider/topic phases succeed."""
        if operation_id is not None:
            atomic = getattr(self.repository, "tombstone_email_for_delete", None)
            if callable(atomic):
                # The production repository performs the lease check and local
                # tombstone in one transaction. Never fall back to a plain
                # tombstone after an atomic implementation is available.
                updated = atomic(
                    int(email_id), int(operation_id), lease_token=lease_token,
                )
                return updated is not None

        row = self.repository.tombstone_email(int(email_id))
        if row is None:
            return False
        if operation_id is not None:
            updated = self.repository.update_delete(
                int(operation_id), success=True, provider_deleted=True,
                topic_deleted=True, tombstoned=True,
                lease_token=lease_token,
            )
            return updated is not None
        return True

    def list_pending_delete_mappings(self, operation_id: int | str) -> list[dict[str, Any]]:
        return [
            {"account_id": item.get("account_id"), "email_id": item.get("email_id"),
             "mailbox": item.get("provider_mailbox") or "INBOX", "uid": item.get("provider_uid"),
             "uidvalidity": item.get("provider_uidvalidity")}
            for item in self.repository.list_delete_targets(int(operation_id), pending_only=True)
            if item.get("provider_uid")
        ]

    def complete_delete_mapping(self, operation_id: int | str, mapping: dict[str, Any], *, success: bool,
                                error: str | None = None, lease_token: str | None = None) -> bool:
        email_id = mapping.get("email_id")
        if email_id is None:
            raise ValueError("delete target email_id is required")
        return self.repository.mark_delete_target_provider_deleted(int(operation_id), int(email_id), success=success,
                                                                    error_message=error, lease_token=lease_token) is not None

    def _delete(self, row: dict[str, Any]) -> DeleteOperation:
        target = self.repository.get_delete_target(int(row["id"])) or {}
        if bool(row.get("tombstoned")):
            state = "tombstoned"
        elif bool(row.get("topic_deleted")):
            state = "telegram_deleted"
        elif bool(row.get("provider_deleted")):
            state = "provider_deleted"
        else:
            state = "queued"
        topic = None
        if target.get("telegram_chat_id") is not None and target.get("telegram_message_thread_id") is not None:
            topic = TelegramTopic(target["telegram_chat_id"], int(target["telegram_message_thread_id"]))
        mappings = [
            {"account_id": item.get("account_id"), "email_id": item.get("email_id"),
             "uid": item["provider_uid"], "mailbox": item.get("provider_mailbox") or "INBOX",
             "uidvalidity": item.get("provider_uidvalidity")}
            for item in target.get("provider_mappings") or () if item.get("provider_uid")
        ]
        # Compatibility with operations created before the immutable target
        # snapshot existed. New thread deletes always carry every live UID.
        if not mappings and target.get("provider_uid"):
            mappings = [{"account_id": row["account_id"], "uid": target["provider_uid"], "mailbox": target.get("provider_mailbox") or "INBOX"}]
        return DeleteOperation(id=str(row["id"]), account_id=row["account_id"], mailbox=target.get("provider_mailbox") or "INBOX", uid=str(target.get("provider_uid") or ""),
                               provider_mapping=mappings, thread_id=target.get("thread_id"), telegram_topic_id=topic,
                               telegram_chat_id=target.get("telegram_chat_id"), email_id=target.get("email_id"),
                               state=state, error=row.get("error_message"),
                               provider_deleted=bool(row.get("provider_deleted")),
                               topic_delete_requested=bool(row.get("topic_delete_requested")),
                               topic_deleted=bool(row.get("topic_deleted")),
                               tombstoned=bool(row.get("tombstoned")),
                               lease_token=row.get("lease_token"),
                               lease_until=row.get("lease_until"),
                               attempts=int(row.get("attempt_count") or 0))

    def _send(self, row: dict[str, Any]) -> SendOperation:
        draft_id = row.get("draft_id")
        if draft_id is None:
            raise ValueError("send operation has no draft")
        conn = self.repository.db.connect()
        try:
            draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (int(draft_id),)).fetchone()
            account = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(row["account_id"]),)).fetchone()
            recipients = conn.execute("SELECT recipient_type, email FROM draft_recipients WHERE draft_id = ? ORDER BY position, id", (int(draft_id),)).fetchall()
            attachments = conn.execute("SELECT * FROM draft_attachments WHERE draft_id = ? ORDER BY id", (int(draft_id),)).fetchall()
        finally:
            conn.close()
        if draft is None or account is None:
            raise KeyError("send operation draft or account is missing")
        grouped = {"to": [], "cc": [], "bcc": []}
        for recipient in recipients:
            grouped[recipient["recipient_type"]].append(recipient["email"])
        hydrated_attachments = tuple(self._attachment(int(draft_id), dict(item)) for item in attachments)
        # SQLite v2 stores markdown and header values; the v2 mail composer
        # renders markdown to HTML and produces the deterministic Message-ID.
        markdown_body = draft["body_markdown"] or ""
        hydrated = MailDraft(operation_id=str(row["id"]), account_id=row["account_id"], from_email=draft["from_identity_email"] or account["email"],
            subject=draft["subject"] or "", markdown_body=markdown_body, text_body=markdown_body,
            html_body=render_markdown_to_html(markdown_body),
            to=tuple(grouped["to"]), cc=tuple(grouped["cc"]), bcc=tuple(grouped["bcc"]), in_reply_to=draft["in_reply_to"],
            references=tuple((draft["references_header"] or "").split()),
            attachments=hydrated_attachments, draft_id=int(draft_id), thread_id=draft["thread_id"],
            transport_account={**dict(account), "password": self.repository.get_account_password(int(account["id"]))})
        return SendOperation(id=str(row["id"]), draft=hydrated, state=row["status"], lease_token=row.get("lease_token"), lease_until=row.get("lease_until"), attempts=int(row.get("attempt_count") or 0), error=row.get("error_message"))

    @staticmethod
    def _attachment(draft_id: int, row: dict[str, Any]) -> Attachment:
        if row.get("status") == "legacy_missing" or row.get("availability") == "legacy_missing":
            raise FileNotFoundError("draft attachment is unavailable")
        local_path = row.get("local_path")
        if not local_path:
            raise FileNotFoundError("draft attachment has no local file")
        data_root = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data")).resolve()
        attachment_root = (data_root / "attachments" / str(int(draft_id))).resolve()
        path = (data_root / str(local_path)).resolve()
        if not path.is_relative_to(attachment_root) or not path.is_file():
            raise FileNotFoundError("draft attachment is unavailable")
        data = path.read_bytes()
        digest = row.get("sha256")
        if digest and hashlib.sha256(data).hexdigest() != digest:
            raise OSError("draft attachment integrity check failed")
        return Attachment(str(row["file_name"]), data, str(row.get("mime_type") or "application/octet-stream"))
