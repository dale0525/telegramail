import sqlite3
import os
import time
import re
from typing import List, Dict, Any, Optional
from app.utils import Logger
from app.utils.decorators import Singleton
from app.database.mixins.topic_tracking import TopicTrackingMixin
from app.database.mixins.drafts import DraftsMixin
from app.database.mixins.email_labels import EmailLabelsMixin
from app.database.emails_schema import ensure_emails_mailbox_schema

logger = Logger().get_logger(__name__)

DEFAULT_DB_PATH = os.path.join(os.getcwd(), "data", "telegramail.db")


def get_db_path() -> str:
    """
    Resolve database path.

    Tests can override via TELEGRAMAIL_DB_PATH to avoid touching real user data.
    """
    return os.getenv("TELEGRAMAIL_DB_PATH") or DEFAULT_DB_PATH


@Singleton
class DBManager(TopicTrackingMixin, DraftsMixin, EmailLabelsMixin):
    """Database manager for handling email operations"""

    def __init__(self):
        """Initialize database manager"""
        # check if database exists
        self._initialize_db()

    def _initialize_db(self) -> None:
        """Initialize database with required tables"""
        db_path = get_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = sqlite3.connect(db_path)
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
    tg_group_id INTEGER,
    imap_monitored_mailboxes TEXT
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
    mailbox TEXT NOT NULL DEFAULT 'INBOX',
    telegram_thread_id TEXT,
    delivered_to TEXT,
    in_reply_to TEXT,
    references_header TEXT,
    UNIQUE(email_account, mailbox, uid)
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

CREATE TABLE IF NOT EXISTS account_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    from_email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    reply_to TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(account_id, from_email)
);

CREATE TABLE IF NOT EXISTS identity_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    suggested_email TEXT NOT NULL,
    source_delivered_to TEXT,
    email_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(account_id, suggested_email)
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    draft_type TEXT NOT NULL,
    from_identity_email TEXT NOT NULL,
    card_message_id INTEGER,
    to_addrs TEXT,
    cc_addrs TEXT,
    bcc_addrs TEXT,
    subject TEXT,
    in_reply_to TEXT,
    references_header TEXT,
    body_markdown TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    remote_id TEXT,
    file_type TEXT,
    file_name TEXT NOT NULL,
    mime_type TEXT,
    size INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    message_type TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(draft_id, message_id)
);
"""
        )

        # Emails: mailbox + UID uniqueness migration (UID is per mailbox in IMAP).
        ensure_emails_mailbox_schema(conn)

        # Accounts: per-account IMAP monitored mailbox override.
        cursor.execute("PRAGMA table_info(accounts)")
        account_columns = {row[1] for row in cursor.fetchall()}
        if "imap_monitored_mailboxes" not in account_columns:
            cursor.execute(
                "ALTER TABLE accounts ADD COLUMN imap_monitored_mailboxes TEXT"
            )

        # Lightweight migrations (SQLite doesn't support ADD COLUMN IF NOT EXISTS).
        cursor.execute("PRAGMA table_info(emails)")
        email_columns = {row[1] for row in cursor.fetchall()}
        if "delivered_to" not in email_columns:
            cursor.execute("ALTER TABLE emails ADD COLUMN delivered_to TEXT")
        if "in_reply_to" not in email_columns:
            cursor.execute("ALTER TABLE emails ADD COLUMN in_reply_to TEXT")
        if "references_header" not in email_columns:
            cursor.execute("ALTER TABLE emails ADD COLUMN references_header TEXT")

        cursor.execute("PRAGMA table_info(drafts)")
        draft_columns = {row[1] for row in cursor.fetchall()}
        if "card_message_id" not in draft_columns:
            cursor.execute("ALTER TABLE drafts ADD COLUMN card_message_id INTEGER")
        if "in_reply_to" not in draft_columns:
            cursor.execute("ALTER TABLE drafts ADD COLUMN in_reply_to TEXT")
        if "references_header" not in draft_columns:
            cursor.execute("ALTER TABLE drafts ADD COLUMN references_header TEXT")

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
        conn = sqlite3.connect(get_db_path())

        # Enable WAL mode which allows concurrent reads
        conn.execute("PRAGMA journal_mode=WAL")

        # Set busy timeout (milliseconds) - how long to wait when db is locked
        conn.execute("PRAGMA busy_timeout=10000")

        return conn

    # --- Identities ---

    def list_account_identities(self, account_id: int) -> List[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM account_identities
                WHERE account_id = ? AND enabled = 1
                ORDER BY is_default DESC, id ASC
                """,
                (int(account_id),),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Error listing account identities: {e}")
            return []

    def upsert_account_identity(
        self,
        *,
        account_id: int,
        from_email: str,
        display_name: str,
        reply_to: Optional[str] = None,
        is_default: bool = False,
        enabled: bool = True,
    ) -> Optional[int]:
        try:
            now = int(time.time())
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO account_identities
                  (account_id, from_email, display_name, reply_to, is_default, enabled, created_at, updated_at)
                VALUES
                  (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, from_email) DO UPDATE SET
                  display_name = excluded.display_name,
                  reply_to = excluded.reply_to,
                  is_default = CASE WHEN excluded.is_default = 1 THEN 1 ELSE account_identities.is_default END,
                  enabled = excluded.enabled,
                  updated_at = excluded.updated_at
                """,
                (
                    int(account_id),
                    (from_email or "").strip().lower(),
                    (display_name or "").strip(),
                    (reply_to or "").strip() or None,
                    1 if is_default else 0,
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )
            conn.commit()

            # Ensure only one default identity per account when setting default.
            if is_default:
                cursor.execute(
                    """
                    UPDATE account_identities
                    SET is_default = 0, updated_at = ?
                    WHERE account_id = ? AND from_email != ?
                    """,
                    (now, int(account_id), (from_email or "").strip().lower()),
                )
                conn.commit()

            # Get identity id
            cursor.execute(
                "SELECT id FROM account_identities WHERE account_id = ? AND from_email = ?",
                (int(account_id), (from_email or "").strip().lower()),
            )
            row = cursor.fetchone()
            conn.close()
            return int(row[0]) if row else None
        except Exception as e:
            logger.error(f"Error upserting account identity: {e}")
            return None

    # --- Identity Suggestions ---

    def upsert_identity_suggestion(
        self,
        *,
        account_id: int,
        suggested_email: str,
        source_delivered_to: Optional[str] = None,
        email_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create or update an identity suggestion.

        Important: if an existing suggestion is already ignored, keep it ignored.
        """
        normalized_email = (suggested_email or "").strip().lower()
        now = int(time.time())
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT * FROM identity_suggestions
            WHERE account_id = ? AND suggested_email = ?
            """,
            (int(account_id), normalized_email),
        )
        existing = cursor.fetchone()

        if existing:
            existing_dict = dict(existing)
            status = existing_dict.get("status") or "pending"
            # Keep ignored to avoid re-prompting.
            if status == "ignored":
                conn.close()
                return {"id": existing_dict["id"], "status": "ignored"}

            cursor.execute(
                """
                UPDATE identity_suggestions
                SET source_delivered_to = ?, email_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    (source_delivered_to or "").strip().lower() or None,
                    int(email_id) if email_id is not None else None,
                    now,
                    int(existing_dict["id"]),
                ),
            )
            conn.commit()
            conn.close()
            return {"id": existing_dict["id"], "status": status}

        cursor.execute(
            """
            INSERT INTO identity_suggestions
              (account_id, suggested_email, source_delivered_to, email_id, status, created_at, updated_at)
            VALUES
              (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                int(account_id),
                normalized_email,
                (source_delivered_to or "").strip().lower() or None,
                int(email_id) if email_id is not None else None,
                now,
                now,
            ),
        )
        suggestion_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return {"id": suggestion_id, "status": "pending"}

    def get_identity_suggestion(self, suggestion_id: int) -> Optional[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM identity_suggestions WHERE id = ?",
                (int(suggestion_id),),
            )
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting identity suggestion: {e}")
            return None

    def mark_identity_suggestion_ignored(self, *, suggestion_id: int) -> bool:
        try:
            now = int(time.time())
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE identity_suggestions
                SET status = 'ignored', updated_at = ?
                WHERE id = ?
                """,
                (now, int(suggestion_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error marking identity suggestion ignored: {e}")
            return False

    def mark_identity_suggestion_accepted(self, *, suggestion_id: int) -> bool:
        try:
            now = int(time.time())
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE identity_suggestions
                SET status = 'accepted', updated_at = ?
                WHERE id = ?
                """,
                (now, int(suggestion_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error marking identity suggestion accepted: {e}")
            return False

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
                "SELECT email_account, uid, mailbox FROM emails WHERE telegram_thread_id = ?",
                (telegram_thread_id,),
            )
            results = cursor.fetchall()  # Fetch all matching records

            conn.close()

            if not results:
                # Return (None, []) if no records found
                return None, []

            # Assuming all rows for a thread belong to the same account
            account_id = results[0][0]
            email_uids: List[str] = []
            for _account_id, uid, mailbox in results:
                if uid is None:
                    continue
                uid_str = str(uid).strip()
                mailbox_str = str(mailbox).strip() if mailbox is not None else ""
                # IMAP UIDs are numeric; filter out synthetic/outgoing ids to avoid
                # attempting server-side deletion for non-INBOX messages.
                if not uid_str or not uid_str.isdigit():
                    continue
                if mailbox_str and mailbox_str.lower() != "inbox":
                    continue
                email_uids.append(uid_str)

            return account_id, email_uids

        except Exception as e:
            logger.error(f"Error getting email uids by Telegram thread ID: {e}")
            raise

    def get_deletion_targets_for_topic(
        self, *, chat_id: int, thread_id: str
    ) -> Dict[int, Dict[str, Any]]:
        """
        Resolve server-side deletion targets for a deleted Telegram topic.

        We scope by chat_id to avoid collisions where different groups may reuse the
        same numeric thread_id.

        Returns:
            Dict[int, Dict[str, List[str]]]: Mapping from account_id to:
                - imap_uids: list of {"uid": str, "mailbox": str} for provider deletions
                - outgoing_message_ids: Message-ID values for synthetic outgoing rows
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id FROM accounts WHERE tg_group_id = ?",
                (int(chat_id),),
            )
            account_ids = [int(r[0]) for r in cursor.fetchall() if r and r[0] is not None]
            if not account_ids:
                return {}

            placeholders = ", ".join(["?"] * len(account_ids))
            cursor.execute(
                f"""
                SELECT email_account, uid, message_id, mailbox
                FROM emails
                WHERE telegram_thread_id = ? AND email_account IN ({placeholders})
                """,
                [str(thread_id), *account_ids],
            )
            rows = cursor.fetchall()

            targets: Dict[int, Dict[str, List[str]]] = {}
            for email_account, uid, message_id, mailbox in rows:
                try:
                    account_id = int(email_account)
                except Exception:
                    continue

                entry = targets.setdefault(
                    account_id,
                    {"imap_uids": [], "outgoing_message_ids": []},
                )

                uid_str = str(uid).strip() if uid is not None else ""
                message_id_str = str(message_id).strip() if message_id is not None else ""
                mailbox_str = str(mailbox).strip() if mailbox is not None else ""
                if not mailbox_str:
                    mailbox_str = "INBOX"

                if uid_str and uid_str.isdigit():
                    item = {"uid": uid_str, "mailbox": mailbox_str}
                    if item not in entry["imap_uids"]:
                        entry["imap_uids"].append(item)
                    continue

                outgoing_mid = message_id_str
                if not outgoing_mid and uid_str.startswith("outgoing:"):
                    outgoing_mid = uid_str[len("outgoing:") :].strip()

                if outgoing_mid and outgoing_mid not in entry["outgoing_message_ids"]:
                    entry["outgoing_message_ids"].append(outgoing_mid)

            # Prune empty entries.
            return {
                aid: v
                for aid, v in targets.items()
                if v["imap_uids"] or v["outgoing_message_ids"]
            }
        except Exception as e:
            logger.error(
                f"Error getting deletion targets for topic (chat_id={chat_id}, thread_id={thread_id}): {e}"
            )
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def find_thread_id_for_reply_headers(
        self,
        *,
        account_id: int,
        in_reply_to: Optional[str],
        references_header: Optional[str],
    ) -> Optional[int]:
        """
        Resolve a Telegram thread_id for an email reply by looking up In-Reply-To / References
        message ids in the local DB.

        This allows incoming replies to be grouped into the correct Telegram topic even when
        subjects change or collide.
        """
        candidates: list[str] = []
        if in_reply_to and str(in_reply_to).strip():
            candidates.append(str(in_reply_to).strip())

        if references_header and str(references_header).strip():
            raw = str(references_header).strip()
            refs = [r.strip() for r in re.findall(r"<[^>]+>", raw) if r.strip()]
            if refs:
                candidates.extend(list(reversed(refs)))
            else:
                parts = [p.strip() for p in raw.replace("\n", " ").split(" ") if p.strip()]
                candidates.extend(list(reversed(parts)))

        if not candidates:
            return None

        # De-duplicate while preserving order.
        seen: set[str] = set()
        uniq: list[str] = []
        for c in candidates:
            if c in seen:
                continue
            seen.add(c)
            uniq.append(c)

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            for message_id in uniq:
                cursor.execute(
                    """
                    SELECT telegram_thread_id
                    FROM emails
                    WHERE email_account = ? AND message_id = ? AND telegram_thread_id IS NOT NULL
                    LIMIT 1
                    """,
                    (int(account_id), message_id),
                )
                row = cursor.fetchone()
                if not row or not row[0]:
                    continue
                try:
                    return int(row[0])
                except Exception:
                    continue
            return None
        except Exception as e:
            logger.error(f"Error finding thread_id by reply headers: {e}")
            return None
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def upsert_outgoing_email(
        self,
        *,
        account_id: int,
        message_id: str,
        telegram_thread_id: int,
        sender: str,
        recipient: str,
        cc: str,
        bcc: str,
        subject: str,
        email_date: Optional[str] = None,
        body_text: Optional[str] = None,
        body_html: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references_header: Optional[str] = None,
    ) -> Optional[int]:
        """
        Insert (or update) a synthetic "outgoing" email row so we can:
        - create a Topic for sent emails, and
        - thread future incoming replies via In-Reply-To / References.
        """
        normalized_mid = (message_id or "").strip()
        if not normalized_mid:
            # Fallback: still create a stable row for this send.
            normalized_mid = f"<outgoing-{int(time.time())}@telegramail>"

        synthetic_uid = f"outgoing:{normalized_mid}"

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT id FROM emails
                WHERE email_account = ? AND message_id = ?
                LIMIT 1
                """,
                (int(account_id), normalized_mid),
            )
            existing = cursor.fetchone()
            if existing and existing[0]:
                email_id = int(existing[0])
                cursor.execute(
                    """
                    UPDATE emails
                    SET sender = ?, recipient = ?, cc = ?, bcc = ?, subject = ?, email_date = ?,
                        body_text = ?, body_html = ?, uid = ?, mailbox = ?, telegram_thread_id = ?,
                        in_reply_to = ?, references_header = ?
                    WHERE id = ?
                    """,
                    (
                        sender,
                        recipient,
                        cc,
                        bcc,
                        subject,
                        email_date,
                        body_text,
                        body_html,
                        synthetic_uid,
                        "OUTGOING",
                        str(int(telegram_thread_id)),
                        in_reply_to,
                        references_header,
                        email_id,
                    ),
                )
                conn.commit()
                return email_id

            cursor.execute(
                """
                INSERT INTO emails
                  (email_account, message_id, sender, recipient, cc, bcc, subject, email_date,
                   body_text, body_html, uid, mailbox, telegram_thread_id, in_reply_to, references_header)
                VALUES
                  (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(account_id),
                    normalized_mid,
                    sender,
                    recipient,
                    cc,
                    bcc,
                    subject,
                    email_date,
                    body_text,
                    body_html,
                    synthetic_uid,
                    "OUTGOING",
                    str(int(telegram_thread_id)),
                    in_reply_to,
                    references_header,
                ),
            )
            email_id = int(cursor.lastrowid)
            conn.commit()
            return email_id
        except Exception as e:
            logger.error(f"Error upserting outgoing email: {e}")
            return None
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def delete_email_by_uid(
        self, account_info: dict[str, Any], uid: str, mailbox: Optional[str] = None
    ) -> bool:
        """
        Delete email by UID from local database

        Args:
            email_account: Email address of the account
            uid: Email UID to delete
            mailbox: IMAP mailbox name (default: INBOX/OUTGOING inferred)

        Returns:
            bool: True if email was deleted, False otherwise
        """
        try:
            uid_str = str(uid).strip()
            mailbox_str = (mailbox or "").strip().strip('"')
            if not mailbox_str:
                mailbox_str = "OUTGOING" if uid_str.startswith("outgoing:") else "INBOX"

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM emails WHERE email_account = ? AND uid = ? AND mailbox = ?",
                (account_info["id"], uid_str, mailbox_str),
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
