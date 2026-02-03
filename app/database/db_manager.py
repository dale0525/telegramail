import sqlite3
import os
from typing import List, Dict, Any, Optional
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
        # check if database exists
        self._initialize_db()

    def _initialize_db(self) -> None:
        """Initialize database with required tables"""
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Create accounts and email table if not exists
        cursor.executescript(
            """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    password TEXT NOT NULL,
    imap_server TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    imap_ssl INTEGER NOT NULL,
    smtp_server TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    smtp_ssl INTEGER NOT NULL,
    alias TEXT NOT NULL,
    tg_group_id INTEGER
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_account INTEGER NOT NULL,
    message_id TEXT,
    sender TEXT,
    recipient TEXT,
    cc TEXT,
    bcc TEXT,
    subject TEXT,
    email_date TEXT,
    body_text TEXT,
    body_html TEXT,
    uid TEXT,
    telegram_thread_id TEXT,
    UNIQUE(email_account, uid)
);
CREATE TABLE IF NOT EXISTS chat_event_cursors (
    chat_id INTEGER PRIMARY KEY,
    last_forum_event_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_topics (
    chat_id INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    deleted_at INTEGER NOT NULL,
    processed_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (chat_id, thread_id)
);
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
        conn = sqlite3.connect(DB_PATH)

        # Enable WAL mode which allows concurrent reads
        conn.execute("PRAGMA journal_mode=WAL")

        # Set busy timeout (milliseconds) - how long to wait when db is locked
        conn.execute("PRAGMA busy_timeout=10000")

        return conn

    def get_accounts(self) -> List[Dict[str, Any]]:
        """
        Get all accounts from the database

        Returns:
            List[Dict[str, Any]]: List of account dictionaries
        """
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM accounts")
            accounts = [dict(row) for row in cursor.fetchall()]

            conn.close()
            return accounts

        except Exception as e:
            logger.error(f"Error getting accounts: {e}")
            return []

    def add_account(self, account: Dict[str, Any]) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO accounts (email, password, imap_server, imap_port, imap_ssl, smtp_server, smtp_port, smtp_ssl, alias, tg_group_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account["email"],
                    account["password"],
                    account["imap_server"],
                    account["imap_port"],
                    account["imap_ssl"],
                    account["smtp_server"],
                    account["smtp_port"],
                    account["smtp_ssl"],
                    account["alias"],
                    account.get("tg_group_id"),
                ),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error adding account: {e}")
            return False

    def remove_account(
        self,
        id: Optional[int | str] = None,
        email: Optional[str] = None,
        smtp_server: Optional[str] = None,
    ) -> bool:
        """
        Remove an account from the database by ID or by email and SMTP server

        Args:
            id: Database ID of the account to remove
            email: Email address of the account to remove
            smtp_server: SMTP server of the account to remove

        Returns:
            bool: True if account was removed, False otherwise
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            id = int(id)

            if id is not None:
                cursor.execute("DELETE FROM accounts WHERE id = ?", (id,))
            elif email is not None and smtp_server is not None:
                cursor.execute(
                    "DELETE FROM accounts WHERE email = ? AND smtp_server = ?",
                    (email, smtp_server),
                )
            else:
                raise ValueError("Either id or email and smtp_server must be specified")

            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error removing account: {e}")
            return False

    def get_account(
        self,
        id: Optional[int | str] = None,
        email: Optional[str] = None,
        smtp_server: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get account by ID or by email and SMTP server

        Args:
            id: Database ID of the account
            email: Email address of the account
            smtp_server: SMTP server of the account

        Returns:
            Optional[Dict[str, Any]]: Account information or None if not found
        """
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if id is not None:
                id = int(id)
                cursor.execute("SELECT * FROM accounts WHERE id = ?", (id,))
            elif email is not None and smtp_server is not None:
                cursor.execute(
                    "SELECT * FROM accounts WHERE email = ? AND smtp_server = ?",
                    (email, smtp_server),
                )
            else:
                raise ValueError("Either id or email and smtp_server must be specified")

            row = cursor.fetchone()

            if not row:
                conn.close()
                return None

            account_dict = {k: row[k] for k in row.keys()}

            conn.close()
            return account_dict

        except Exception as e:
            logger.error(f"Error getting account: {e}")
            return None

    def update_account(
        self,
        updates: Dict[str, Any],
        id: Optional[int | str] = None,
        email: Optional[str] = None,
        smtp_server: Optional[str] = None,
    ):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            id = int(id)

            if id is not None:
                # Only update keys that are present in updates
                if updates:
                    set_clause = ", ".join([f"{key} = ?" for key in updates.keys()])
                    params = list(updates.values()) + [id]
                    cursor.execute(
                        f"UPDATE accounts SET {set_clause} WHERE id = ?", params
                    )
            elif email is not None and smtp_server is not None:
                # Only update keys that are present in updates
                if updates:
                    set_clause = ", ".join([f"{key} = ?" for key in updates.keys()])
                    params = list(updates.values()) + [email]
                    cursor.execute(
                        f"UPDATE accounts SET {set_clause} WHERE email = ?", params
                    )
            else:
                raise ValueError("Either id or email must be specified")

            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating account: {e}")
            return False

    def get_email_uid_by_telegram_thread_id(
        self, telegram_thread_id: str
    ) -> tuple[Optional[int], List[str]]:
        """
        Get email account ID and associated email UIDs by Telegram thread ID.

        Args:
            telegram_thread_id: Telegram thread ID to search for.

        Returns:
            tuple[Optional[int], List[str]]: A tuple containing the account ID
            and a list of email UIDs associated with the thread ID.
            Returns (None, []) if no matching records are found.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(
                "SELECT email_account, uid FROM emails WHERE telegram_thread_id = ?",
                (telegram_thread_id,),
            )
            results = cursor.fetchall()  # Fetch all matching records

            conn.close()

            if not results:
                # Return (None, []) if no records found
                return None, []

            # Assuming all UIDs for a thread belong to the same account
            account_id = results[0][0]
            email_uids = [row[1] for row in results]

            return account_id, email_uids

        except Exception as e:
            logger.error(f"Error getting email uids by Telegram thread ID: {e}")
            raise

    def get_chat_event_cursor(self, chat_id: int) -> int:
        """
        Get the last processed forum event_id for a chat.

        Args:
            chat_id: Telegram group chat ID.

        Returns:
            int: last processed event_id; 0 if not set.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_forum_event_id FROM chat_event_cursors WHERE chat_id = ?",
                (int(chat_id),),
            )
            row = cursor.fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"Error getting chat event cursor for chat {chat_id}: {e}")
            return 0

    def set_chat_event_cursor(self, chat_id: int, event_id: int) -> bool:
        """
        Upsert the last processed forum event_id for a chat.

        Args:
            chat_id: Telegram group chat ID.
            event_id: Latest processed event_id.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO chat_event_cursors (chat_id, last_forum_event_id)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET last_forum_event_id = excluded.last_forum_event_id
                """,
                (int(chat_id), int(event_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error setting chat event cursor for chat {chat_id} to {event_id}: {e}"
            )
            return False

    def upsert_deleted_topic(
        self, chat_id: int, thread_id: str, event_id: int, deleted_at: int
    ) -> bool:
        """
        Record a deleted topic so it can be processed reliably even after restarts.

        Args:
            chat_id: Telegram group chat ID.
            thread_id: Telegram forum topic thread ID.
            event_id: Chat event log event_id that recorded the deletion.
            deleted_at: Unix timestamp of deletion event.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO deleted_topics (chat_id, thread_id, event_id, deleted_at, processed_at, attempts, last_error)
                VALUES (?, ?, ?, ?, NULL, 0, NULL)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    deleted_at = excluded.deleted_at
                """,
                (int(chat_id), str(thread_id), int(event_id), int(deleted_at)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error upserting deleted topic (chat_id={chat_id}, thread_id={thread_id}): {e}"
            )
            return False

    def list_pending_deleted_topics(self, chat_id: int) -> List[Dict[str, Any]]:
        """
        List topics deleted in Telegram that still need IMAP deletion processing.

        Args:
            chat_id: Telegram group chat ID.

        Returns:
            List[Dict[str, Any]]: Rows containing thread_id, attempts, last_error, deleted_at.
        """
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT chat_id, thread_id, attempts, last_error, deleted_at
                FROM deleted_topics
                WHERE chat_id = ? AND processed_at IS NULL
                ORDER BY deleted_at ASC
                """,
                (int(chat_id),),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Error listing pending deleted topics for chat {chat_id}: {e}")
            return []

    def mark_deleted_topic_processed(self, chat_id: int, thread_id: str) -> bool:
        """
        Mark a deleted topic as processed.

        Args:
            chat_id: Telegram group chat ID.
            thread_id: Telegram thread ID.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE deleted_topics
                SET processed_at = strftime('%s','now'), last_error = NULL
                WHERE chat_id = ? AND thread_id = ?
                """,
                (int(chat_id), str(thread_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error marking deleted topic processed (chat_id={chat_id}, thread_id={thread_id}): {e}"
            )
            return False

    def record_deleted_topic_failure(
        self, chat_id: int, thread_id: str, error: str
    ) -> bool:
        """
        Record a failure while processing a deleted topic.

        Args:
            chat_id: Telegram group chat ID.
            thread_id: Telegram thread ID.
            error: Error string for debugging.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE deleted_topics
                SET attempts = attempts + 1, last_error = ?
                WHERE chat_id = ? AND thread_id = ? AND processed_at IS NULL
                """,
                (str(error), int(chat_id), str(thread_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error recording deleted topic failure (chat_id={chat_id}, thread_id={thread_id}): {e}"
            )
            return False

    def delete_email_by_uid(self, account_info: dict[str, Any], uid: str) -> bool:
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
                (account_info["id"], uid),
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

    def update_thread_id_in_db(self, email_id: int, thread_id: int) -> bool:
        """
        Update the telegram_thread_id for an email in the database

        Args:
            email_id: Database ID of the email
            thread_id: Telegram message thread ID

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE emails SET telegram_thread_id = ? WHERE id = ?",
                (str(thread_id), email_id),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating thread ID in database: {e}")
            return False
