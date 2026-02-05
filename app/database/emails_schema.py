import sqlite3

from app.utils import Logger

logger = Logger().get_logger(__name__)


def _get_table_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall() if row and len(row) > 1}


def _has_unique_index(
    cursor: sqlite3.Cursor, *, table_name: str, columns: list[str]
) -> bool:
    cursor.execute(f"PRAGMA index_list({table_name})")
    for row in cursor.fetchall() or []:
        if not row or len(row) < 3:
            continue
        index_name = row[1]
        is_unique = bool(row[2])
        if not is_unique:
            continue
        # index_info rows: (seqno, cid, name)
        cursor.execute(f'PRAGMA index_info("{index_name}")')
        idx_cols = [r[2] for r in cursor.fetchall() or [] if r and len(r) > 2]
        if idx_cols == columns:
            return True
    return False


def ensure_emails_mailbox_schema(conn: sqlite3.Connection) -> None:
    """
    Ensure the `emails` table supports multiple mailboxes.

    IMAP UIDs are only unique within a mailbox, so we store the source mailbox and
    enforce uniqueness on (email_account, mailbox, uid).
    """
    cursor = conn.cursor()
    columns = _get_table_columns(cursor, "emails")

    has_mailbox = "mailbox" in columns
    has_new_unique = _has_unique_index(
        cursor, table_name="emails", columns=["email_account", "mailbox", "uid"]
    )
    has_old_unique = _has_unique_index(
        cursor, table_name="emails", columns=["email_account", "uid"]
    )

    if has_mailbox and has_new_unique and not has_old_unique:
        return

    if not has_mailbox or has_old_unique or not has_new_unique:
        logger.info("Migrating emails table to add mailbox-scoped UID uniqueness")
        conn.execute("BEGIN")
        try:
            cursor.execute("ALTER TABLE emails RENAME TO emails__old")

            cursor.execute(
                """
                CREATE TABLE emails (
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
                """
            )

            old_columns = _get_table_columns(cursor, "emails__old")

            def col(name: str, fallback_sql: str = "NULL") -> str:
                return name if name in old_columns else fallback_sql

            mailbox_expr = col(
                "mailbox",
                "CASE WHEN uid LIKE 'outgoing:%' THEN 'OUTGOING' ELSE 'INBOX' END",
            )

            cursor.execute(
                f"""
                INSERT INTO emails
                  (id, email_account, message_id, sender, recipient, cc, bcc, subject, email_date,
                   body_text, body_html, uid, mailbox, telegram_thread_id, delivered_to, in_reply_to, references_header)
                SELECT
                  {col("id")}, {col("email_account")}, {col("message_id")}, {col("sender")}, {col("recipient")},
                  {col("cc")}, {col("bcc")}, {col("subject")}, {col("email_date")},
                  {col("body_text")}, {col("body_html")}, {col("uid")}, {mailbox_expr},
                  {col("telegram_thread_id")}, {col("delivered_to")}, {col("in_reply_to")}, {col("references_header")}
                FROM emails__old
                """
            )

            cursor.execute("DROP TABLE emails__old")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

