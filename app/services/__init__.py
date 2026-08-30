from .mail import MailService
from .mail_repository import InMemoryMailRepository, MailRepository

__all__ = ["InMemoryMailRepository", "MailRepository", "MailService", "V2MailRepositoryAdapter"]


def __getattr__(name: str):
    # Keep the service card importable by the mail integration without importing
    # the repository adapter back through app.integrations.mail.telegram.
    if name == "V2MailRepositoryAdapter":
        from .v2_repository_adapter import V2MailRepositoryAdapter
        return V2MailRepositoryAdapter
    raise AttributeError(name)
