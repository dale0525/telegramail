"""Application-level entry points for v2 mail operations."""
from __future__ import annotations

from typing import Any

from app.integrations.mail.compose import MailComposer
from app.integrations.mail.types import DeleteOperation, MailDraft, SendOperation


class MailService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def queue_send(self, draft: MailDraft) -> SendOperation:
        # Compose before persistence: malformed recipients never create a queue row.
        _, message_id, _ = MailComposer.compose(draft)
        normalized = MailDraft(**{**draft.__dict__, "message_id": message_id})
        operation = SendOperation(id=draft.operation_id, draft=normalized)
        if not self.repository.enqueue_send(operation):
            existing = getattr(self.repository, "get_send", lambda _: None)(operation.id)
            return existing or operation
        return operation

    def queue_delete(self, operation: DeleteOperation) -> DeleteOperation:
        if not self.repository.enqueue_delete(operation):
            existing = getattr(self.repository, "get_delete", lambda _: None)(operation.id)
            return existing or operation
        return operation
