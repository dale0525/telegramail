from typing import Dict, List, Optional, Any
from app.utils import Logger
from app.utils.decorators import Singleton
from app.database import DBManager

logger = Logger().get_logger(__name__)


@Singleton
class AccountManager:
    """Email accounts manager"""

    def __init__(self):
        self.db_manager = DBManager()

    def add_account(self, account: Dict[str, Any]) -> bool:
        """
        add new account

        Args:
            account: account info dict containing following keys:
                    - email: email address
                    - password: email password
                    - smtp_server: SMTP server
                    - smtp_port: SMTP port
                    - smtp_ssl: bool, use SSL/TLS for SMTP
                    - imap_server: IMAP server
                    - imap_port: IMAP port
                    - imap_ssl: bool, use SSL/TLS for IMAP
                    - alias: alias name of the account
                    - tg_group_id: telegram group chat id

        Returns:
            bool: success or not
        """
        # check required fields
        required_fields = [
            "email",
            "password",
            "smtp_server",
            "smtp_port",
            "smtp_ssl",
            "imap_server",
            "imap_port",
            "imap_ssl",
            "alias",
            "tg_group_id",
        ]
        for field in required_fields:
            if field not in account:
                logger.error(f"Missing required field: {field}")
                return False

        existing_accounts = self.db_manager.get_accounts()
        # check if the same email addr. exists
        for existing in existing_accounts:
            if (
                existing["email"] == account["email"]
                and existing["smtp_server"] == account["smtp_server"]
            ):
                logger.warning(
                    f"Account with email {account['email']} and server {account['smtp_server']} already exists"
                )
                return False

        # add account
        if not self.db_manager.add_account(account):
            logger.error("Failed to add account to database")
            return False

        # Create default sending identity for this account (From = login email).
        try:
            created = self.db_manager.get_account(
                email=account["email"], smtp_server=account["smtp_server"]
            )
            if created:
                self.db_manager.upsert_account_identity(
                    account_id=created["id"],
                    from_email=account["email"],
                    display_name=account.get("alias") or account["email"],
                    is_default=True,
                )
        except Exception as e:
            logger.error(f"Failed to create default identity for account: {e}")
        return True

    def remove_account(
        self,
        id: Optional[int] = None,
        email: Optional[str] = None,
        smtp_server: Optional[str] = None,
    ) -> bool:
        return self.db_manager.remove_account(
            id=id, email=email, smtp_server=smtp_server
        )

    def get_account(
        self,
        id: Optional[int] = None,
        email: Optional[str] = None,
        smtp_server: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get an account by ID, email, or email and SMTP server."""
        return self.db_manager.get_account(id=id, email=email, smtp_server=smtp_server)

    def get_all_accounts(self) -> List[Dict[str, Any]]:
        return self.db_manager.get_accounts()

    def update_account(
        self,
        updates: Dict[str, Any],
        id: Optional[int] = None,
        email: Optional[str] = None,
        smtp_server: Optional[str] = None,
    ) -> bool:
        return self.db_manager.update_account(
            updates=updates, id=id, email=email, smtp_server=smtp_server
        )
