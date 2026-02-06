from typing import Any

from app.email_utils.imap_client import IMAPClient
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def fetch_account_emails(account: dict[str, Any]) -> int:
    imap_client = IMAPClient(account)
    return await imap_client.fetch_unread_emails()


async def fetch_account_emails_safe(account: dict[str, Any]) -> tuple[str, int, str]:
    email_addr = account.get("email", "")
    try:
        count = await fetch_account_emails(account)
        return email_addr, count, ""
    except Exception as e:
        logger.error(f"Error fetching emails for {email_addr}: {e}")
        return email_addr, 0, str(e)
