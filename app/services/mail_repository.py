"""Small repository protocol and an in-memory reference implementation.

Production repositories may expose the same names with database transactions;
workers intentionally depend only on this duck-typed surface.
"""
from __future__ import annotations

import time
import uuid
import re
from typing import Any, Protocol, runtime_checkable

from app.integrations.mail.types import DeleteOperation, IncomingMail, SendOperation


@runtime_checkable
class MailRepository(Protocol):
    def enqueue_send(self, operation: SendOperation) -> bool: ...
    def claim_send(self, operation_id: str, worker_id: str, lease_seconds: float, now: float | None = None) -> SendOperation | None: ...
    def complete_send(self, operation_id: str, lease_token: str, state: str, *, error: str | None = None, now: float | None = None) -> bool: ...


class InMemoryMailRepository:
    """Deterministic store used by unit tests and as protocol documentation."""
    def __init__(self) -> None:
        self.sends: dict[str, SendOperation] = {}
        self.account_statuses: dict[str, dict[str, Any]] = {}
        self.incoming: dict[tuple[str, str, str, str], IncomingMail] = {}
        self.contacts: set[tuple[str, str]] = set()
        self.message_threads: dict[tuple[str, str], str] = {}
        self.subject_threads: dict[tuple[str, str], str] = {}
        self.thread_accounts: dict[str, str] = {}
        self.email_threads: dict[tuple[str, str, str, str], str] = {}
        self.ingestion_leases: dict[tuple[str, str], tuple[str, float]] = {}
        self.deletes: dict[str, DeleteOperation] = {}
        self.tombstones: set[tuple[str, str, str, str]] = set()

        self.projection_waiting: set[tuple[str, str, str, str]] = set()
        self.projection_jobs: dict[str, dict[str, Any]] = {}
        self.labels: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.imap_cursors: dict[tuple[str, str], dict[str, Any]] = {}

    def update_account_connection_status(self, account_id: Any, *, ok: bool, error: str | None = None) -> bool:
        self.account_statuses[str(account_id)] = {
            "connection_status": "connected" if ok else "failed",
            "connection_error": None if ok else (error or "connection"),
        }
        return True

    def enqueue_send(self, operation: SendOperation) -> bool:
        if operation.id in self.sends:
            return False
        self.sends[operation.id] = operation
        return True

    def get_send(self, operation_id: str) -> SendOperation | None:
        return self.sends.get(operation_id)

    def claim_send(self, operation_id: str, worker_id: str, lease_seconds: float, now: float | None = None) -> SendOperation | None:
        now = time.time() if now is None else now
        op = self.sends.get(operation_id)
        if op is None or op.state in {"sent", "ambiguous"}:
            return None
        can_claim = op.state == "queued" or (op.state == "sending" and (op.lease_until or 0) <= now)
        if not can_claim:
            return None
        op.state, op.lease_token, op.lease_until = "sending", f"{worker_id}:{uuid.uuid4().hex}", now + lease_seconds
        op.attempts += 1
        return op

    def claim_next_send(self, worker_id: str, lease_seconds: float, now: float | None = None) -> SendOperation | None:
        for op_id in list(self.sends):
            op = self.claim_send(op_id, worker_id, lease_seconds, now)
            if op:
                return op
        return None

    def complete_send(self, operation_id: str, lease_token: str, state: str, *, error: str | None = None, now: float | None = None) -> bool:
        op = self.sends.get(operation_id)
        if not op or op.state != "sending" or op.lease_token != lease_token:
            return False
        op.state, op.error, op.lease_token, op.lease_until = state, error, None, None
        if state == "sent":
            op.sent_at = time.time() if now is None else now
        return True

    def acquire_ingestion_lease(self, account_id: Any, mailbox: str, owner_id: str, lease_seconds: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        key = (str(account_id), mailbox.casefold())
        current = self.ingestion_leases.get(key)
        if current and current[1] > now and current[0] != owner_id:
            return False
        self.ingestion_leases[key] = (owner_id, now + lease_seconds)
        return True

    def release_ingestion_lease(self, account_id: Any, mailbox: str, owner_id: str) -> bool:
        key = (str(account_id), mailbox.casefold())
        if self.ingestion_leases.get(key, (None,))[0] != owner_id:
            return False
        self.ingestion_leases.pop(key, None)
        return True

    def max_ingested_uid(self, account_id: Any, mailbox: str) -> int:
        values = [int(uid) for aid, box, _, uid in self.incoming if aid == str(account_id) and box == mailbox.casefold() and uid.isdigit()]
        return max(values, default=0)

    def get_imap_cursor(self, account_id: Any, mailbox: str) -> dict[str, Any]:
        key = (str(account_id), mailbox.casefold())
        cursor = self.imap_cursors.get(key)
        if cursor is None:
            cursor = {"uidvalidity": None, "last_uid": self.max_ingested_uid(account_id, mailbox)}
            self.imap_cursors[key] = cursor
        return dict(cursor)

    def reset_imap_cursor(self, account_id: Any, mailbox: str, uidvalidity: str, last_uid: int = 0) -> dict[str, Any]:
        cursor = {"uidvalidity": str(uidvalidity), "last_uid": int(last_uid), "reset": True}
        self.imap_cursors[(str(account_id), mailbox.casefold())] = cursor
        return dict(cursor)

    def advance_imap_cursor(self, account_id: Any, mailbox: str, uidvalidity: str, last_uid: int) -> dict[str, Any]:
        key = (str(account_id), mailbox.casefold())
        current = self.get_imap_cursor(account_id, mailbox)
        if current["uidvalidity"] not in (None, str(uidvalidity)):
            return {**current, "reset_required": True}
        cursor = {"uidvalidity": str(uidvalidity), "last_uid": max(int(current["last_uid"]), int(last_uid)), "reset_required": False}
        self.imap_cursors[key] = cursor
        return dict(cursor)

    def insert_incoming_if_absent(self, mail: IncomingMail) -> bool:
        key = _incoming_key(mail)
        if key in self.incoming or key in self.tombstones:
            return False
        self.incoming[key] = mail
        return True

    def upsert_contact(self, account_id: Any, address: str) -> None:
        if address:
            self.contacts.add((str(account_id), address.casefold()))

    def update_incoming_labels(self, mail: IncomingMail, analysis: dict[str, Any]) -> bool:
        key = _incoming_key(mail)
        if key not in self.incoming:
            return False
        self.labels[key] = {"category": analysis.get("category") or "other", "priority": analysis.get("priority") or "medium",
                            "confidence": analysis.get("category_confidence"), "summary": analysis.get("summary")}
        return True

    def mark_projection_waiting(self, mail: IncomingMail) -> None:
        self.enqueue_projection(mail)

    def enqueue_projection(self, mail: IncomingMail) -> None:
        key = _incoming_key(mail)
        self.projection_waiting.add(key)
        job_id = "projection:" + ":".join(key)
        self.projection_jobs.setdefault(job_id, {"mail": mail, "status": "queued", "account": {"id": mail.account_id}})

    def claim_next_projection(self, worker_id: str | None = None, lease_seconds: float = 60.0):
        from app.integrations.mail.types import ProjectionJob
        for job_id, job in self.projection_jobs.items():
            if job["status"] in {"queued", "failed"}:
                job["status"] = "delivering"
                return ProjectionJob(job_id, job["mail"], job["account"])
        return None

    def complete_projection(
        self,
        projection_id: str,
        success: bool,
        telegram_message_id: Any = None,
        *,
        message_kind: str | None = None,
    ) -> bool:
        job = self.projection_jobs.get(str(projection_id))
        if not job or job["status"] != "delivering":
            return False
        job["status"] = "delivered" if success else "failed"
        if success and message_kind:
            job["message_kind"] = str(message_kind)
        return True

    def mark_projection_ambiguous(self, projection_id: str, *, lease_token: str | None = None) -> bool:
        job = self.projection_jobs.get(str(projection_id))
        if not job or job["status"] != "delivering":
            return False
        job["status"] = "ambiguous"
        return True

    def resolve_thread(self, mail: IncomingMail) -> str | None:
        aid = str(mail.account_id)
        for mid in ((mail.in_reply_to,) + tuple(reversed(mail.references))):
            if mid and (aid, mid.strip().casefold()) in self.message_threads:
                return self.message_threads[(aid, mid.strip().casefold())]
        subject = _thread_subject(mail.subject)
        return self.subject_threads.get((aid, subject)) if subject else None

    def assign_thread(self, mail: IncomingMail, thread_id: str) -> None:
        aid = str(mail.account_id)
        normalized_thread = str(thread_id)
        owner = self.thread_accounts.get(normalized_thread)
        if owner is not None and owner != aid:
            raise ValueError("thread does not belong to account")
        self.thread_accounts[normalized_thread] = aid
        self.email_threads[_incoming_key(mail)] = normalized_thread
        if mail.message_id:
            self.message_threads[(aid, mail.message_id.strip().casefold())] = normalized_thread
        subject = _thread_subject(mail.subject)
        if subject:
            self.subject_threads[(aid, subject)] = normalized_thread

    def rollback_new_topic_assignment(self, mail: IncomingMail, topic: Any) -> bool:
        key = _incoming_key(mail)
        assigned = self.email_threads.get(key)
        if assigned is None or str(assigned) != str(topic):
            return False
        if any(other != key and value == assigned for other, value in self.email_threads.items()):
            return False
        self.email_threads.pop(key, None)
        if mail.message_id:
            self.message_threads.pop((str(mail.account_id), mail.message_id.strip().casefold()), None)
        subject = _thread_subject(mail.subject)
        if subject:
            self.subject_threads.pop((str(mail.account_id), subject), None)
        if not any(value == assigned for value in self.email_threads.values()):
            self.thread_accounts.pop(str(assigned), None)
        return True

    def enqueue_delete(self, operation: DeleteOperation) -> bool:
        if operation.id in self.deletes:
            return False
        self.deletes[operation.id] = operation
        return True

    def claim_next_delete(self, worker_id: str, lease_seconds: float, now: float | None = None) -> DeleteOperation | None:
        # Delete state itself is durable progress, so no separate lease is needed
        # in this reference store. A database implementation should CAS this claim.
        for op in self.deletes.values():
            if op.state not in {"tombstoned", "failed"}:
                return op
        return None

    def update_delete(self, operation_id: str, state: str, *, error: str | None = None,
                      lease_token: str | None = None) -> bool:
        op = self.deletes.get(operation_id)
        if not op:
            return False
        op.state, op.error = state, error
        if state == "provider_deleted":
            op.provider_deleted = True
        elif state == "telegram_deleted":
            op.topic_deleted = True
        elif state == "tombstoned":
            op.provider_deleted = True
            op.topic_deleted = True
            op.tombstoned = True
        if state == "tombstoned":
            self.tombstones.add((str(op.account_id), op.mailbox.casefold(), "", str(op.uid)))
        return True

    def mark_delete_topic_deleted(self, operation_id: str) -> bool:
        op = self.deletes.get(operation_id)
        if not op:
            return False
        op.topic_deleted = True
        return True

    def mark_delete_topic_requested(self, operation_id: str) -> bool:
        op = self.deletes.get(operation_id)
        if not op:
            return False
        op.topic_delete_requested = True
        return True

    def tombstone_email(self, email_id: int | str, operation_id: str | None = None,
                        *, lease_token: str | None = None) -> bool:
        """Reference-store equivalent of the SQLite threadless tombstone."""
        target = str(email_id)
        found = False
        for key, mail in self.incoming.items():
            if str(getattr(mail, "email_id", "")) != target:
                continue
            self.tombstones.add(key)
            found = True
        if operation_id is not None and operation_id in self.deletes:
            self.update_delete(operation_id, "tombstoned", lease_token=lease_token)
        return found


def _incoming_key(mail: IncomingMail) -> tuple[str, str, str, str]:
    return (str(mail.account_id), mail.mailbox.casefold(), str(mail.uidvalidity or ""), str(mail.uid))


def _thread_subject(subject: str) -> str:
    value = " ".join((subject or "").strip().split())
    value = re.sub(r"^(?:(?:re|fw|fwd|回复|转发)[:：]\s*)+", "", value, flags=re.IGNORECASE).strip()
    return " ".join(value.casefold().split()) if value else ""
