import sqlite3
import os
from typing import List, Dict, Any, Optional, Tuple
from app.utils import Logger
from app.utils.decorators import Singleton

logger = Logger().get_logger(__name__)

# Database constants
DB_PATH = os.path.join(os.getcwd(), "data", "telegramail.db")


@Singleton
class DBManager:
    """Database manager for handling email operations"""

    def __init__(self):
        """Initialize database manager"""
        self._initialize_db()

    def _initialize_db(self) -> None:
        """Initialize database with required tables"""
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Create emails table if not exists
        cursor.execute(
            """
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_account TEXT NOT NULL,
            message_id TEXT,
            sender TEXT,
            recipient TEXT,
            cc TEXT,
            bcc TEXT,
            subject TEXT,
            date TEXT,
            body_text TEXT,
            body_html TEXT,
            timestamp INTEGER,
            uid TEXT,
            telegram_thread_id TEXT,
            UNIQUE(email_account, uid)
        )
        """
        )

        conn.commit()
        conn.close()

    def _get_connection(self):
        """
        Get a database connection with timeout settings to help avoid locks

        Returns:
            sqlite3.Connection: SQLite database connection
        """
        # Set timeout (in seconds) for acquiring a lock
        # Use WAL mode to reduce locking issues
        conn = sqlite3.connect(DB_PATH, timeout=20.0)

        # Enable WAL mode which allows concurrent reads
        conn.execute("PRAGMA journal_mode=WAL")

        # Set busy timeout (milliseconds) - how long to wait when db is locked
        conn.execute("PRAGMA busy_timeout=10000")

        return conn

    def get_email_by_id(self, email_id: int) -> Optional[Dict[str, Any]]:
        """
        Get email by ID

        Args:
            email_id: Database ID of the email

        Returns:
            Optional[Dict[str, Any]]: Email information or None if not found
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM emails WHERE id = ?", (email_id,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                return None

            email_dict = {k: row[k] for k in row.keys()}

            conn.close()
            return email_dict

        except Exception as e:
            logger.error(f"Error getting email by ID: {e}")
            return None

    def get_email_uid_by_telegram_thread_id(
        self, telegram_thread_id: str
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Get email account and UID by Telegram thread ID

        Args:
            telegram_thread_id: Telegram thread ID to search for

        Returns:
            tuple[Optional[str], Optional[str]]: (email_account, uid) or (None, None) if not found
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(
                "SELECT email_account, uid FROM emails WHERE telegram_thread_id = ?",
                (telegram_thread_id,),
            )
            result = cursor.fetchone()

            conn.close()

            if result:
                return result[0], result[1]
            return None, None

        except Exception as e:
            logger.error(f"Error getting email uid by Telegram thread ID: {e}")
            return None, None

    def delete_email_by_uid(self, email_account: str, uid: str) -> bool:
        """
        Delete email by UID from local database

        Args:
            email_account: Email address of the account
            uid: Email UID to delete

        Returns:
            bool: True if email was deleted, False otherwise
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM emails WHERE email_account = ? AND uid = ?",
                (email_account, uid),
            )
            conn.commit()
            rows_affected = cursor.rowcount
            conn.close()

            if rows_affected > 0:
                logger.info(f"Removed email with UID {uid} from local database")
                return True
            else:
                logger.warning(f"No email with UID {uid} found in database to delete")
                return False
        except Exception as e:
            logger.error(f"Error deleting email with UID {uid} from database: {e}")
            return False
