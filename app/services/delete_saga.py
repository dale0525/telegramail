"""Deletion saga service import."""
from app.workers.mail import MailDeleteWorker

DeleteSaga = MailDeleteWorker

__all__ = ["DeleteSaga", "MailDeleteWorker"]
