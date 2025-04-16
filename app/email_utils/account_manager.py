import json
import os
from typing import Dict, List, Optional, Any
from app.utils.logger import Logger

logger = Logger().get_logger(__name__)

ACCOUNTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "accounts.json"
)


class AccountManager:
    """Email accounts manager"""

    _instance = None

    @classmethod
    def get_instance(cls) -> "AccountManager":
        """get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        """init class"""
        self.accounts = []
        self._load_accounts()

        # make sure data folder exists
        os.makedirs(os.path.dirname(ACCOUNTS_FILE), exist_ok=True)

    def _load_accounts(self) -> None:
        """load account info from json file"""
        if not os.path.exists(ACCOUNTS_FILE):
            self.accounts = []
            return

        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.accounts = data
        except Exception as e:
            logger.error(f"Failed to load accounts: {e}")
            self.accounts = []

    def _save_accounts(self) -> None:
        """save account info to json file"""
        try:
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.accounts, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save accounts: {e}")

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
        ]
        for field in required_fields:
            if field not in account:
                logger.error(f"Missing required field: {field}")
                return False

        # check if the same email addr. exists
        for existing in self.accounts:
            if existing["email"] == account["email"]:
                logger.warning(f"Account with email {account['email']} already exists")
                return False

        # add account
        self.accounts.append(account)
        self._save_accounts()
        return True

    def remove_account(self, email: str) -> bool:
        """
        delete email account

        Args:
            email: email addr. to delete

        Returns:
            bool: success or not
        """
        for i, account in enumerate(self.accounts):
            if account["email"] == email:
                del self.accounts[i]
                self._save_accounts()
                return True

        logger.warning(f"Account with email {email} not found")
        return False

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        """
        get specified email account

        Args:
            email: email addr.

        Returns:
            Optional[Dict[str, Any]]: Email account info. Returns None if email addr. not exists.
        """
        for account in self.accounts:
            if account["email"] == email:
                return account.copy()
        return None

    def get_all_accounts(self) -> List[Dict[str, Any]]:
        """
        get all email accounts

        Returns:
            List[Dict[str, Any]]: a list of all accounts
        """
        return [account.copy() for account in self.accounts]

    def update_account(self, email: str, updates: Dict[str, Any]) -> bool:
        """
        update email account info

        Args:
            email: the email addr. to update
            updates: key-value pairs to update

        Returns:
            bool: success or not
        """
        for account in self.accounts:
            if account["email"] == email:
                account.update(updates)
                self._save_accounts()
                return True

        logger.warning(f"Account with email {email} not found")
        return False
