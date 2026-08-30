"""Ingestion is executed by :class:`app.workers.MailIngestionWorker`."""
from app.workers.mail import MailIngestionWorker

__all__ = ["MailIngestionWorker"]
