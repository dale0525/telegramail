"""Compatibility import for the v2 send queue service."""
from .mail import MailService

OutboxService = MailService

__all__ = ["MailService", "OutboxService"]
