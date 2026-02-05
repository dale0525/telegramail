import os
import sqlite3
import tempfile
import unittest


class TestDbEmailsMailboxMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "telegramail-test.db")
        os.environ["TELEGRAMAIL_DB_PATH"] = self.db_path

        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager

        DBManager.reset_instance()
        AccountManager.reset_instance()

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    def test_new_db_has_mailbox_column_and_unique_per_mailbox(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()

        cur.execute("PRAGMA table_info(emails)")
        columns = {row[1] for row in cur.fetchall()}
        self.assertIn("mailbox", columns)

        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, mailbox)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<m1@example.com>", "Hello", "42", "INBOX"),
        )
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, mailbox)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<m2@example.com>", "Archived", "42", "Archive"),
        )

        # Default mailbox should be INBOX for rows inserted without explicit mailbox.
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid)
            VALUES (?, ?, ?, ?)
            """,
            (1, "<m3@example.com>", "Default box", "99"),
        )
        conn.commit()

        cur.execute("SELECT mailbox FROM emails WHERE email_account = ? AND uid = ?", (1, "99"))
        row = cur.fetchone()
        self.assertEqual(row[0], "INBOX")

        # Duplicate uid within the same mailbox must violate UNIQUE(email_account, mailbox, uid).
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """
                INSERT INTO emails (email_account, message_id, subject, uid, mailbox)
                VALUES (?, ?, ?, ?, ?)
                """,
                (1, "<m4@example.com>", "Dup", "42", "INBOX"),
            )
            conn.commit()

        conn.close()

    def test_existing_db_without_mailbox_is_migrated(self):
        # Create a legacy schema DB (no mailbox column, UNIQUE(email_account, uid)).
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_account INTEGER NOT NULL,
                message_id TEXT,
                subject TEXT,
                uid TEXT,
                telegram_thread_id TEXT,
                UNIQUE(email_account, uid)
            );
            """
        )
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<in1@example.com>", "Hello", "42", "123"),
        )
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<m1@example.com>", "OUT", "outgoing:<m1@example.com>", "123"),
        )
        conn.commit()
        conn.close()

        from app.database import DBManager

        DBManager.reset_instance()
        db = DBManager()

        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT uid, mailbox FROM emails WHERE email_account = ? ORDER BY id ASC", (1,))
        rows = cur.fetchall()
        self.assertEqual(rows[0][0], "42")
        self.assertEqual(rows[0][1], "INBOX")
        self.assertEqual(rows[1][0], "outgoing:<m1@example.com>")
        self.assertEqual(rows[1][1], "OUTGOING")

        # After migration, same uid can exist in a different mailbox.
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, mailbox)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<in2@example.com>", "Archived", "42", "Archive"),
        )
        conn.commit()

        conn.close()

