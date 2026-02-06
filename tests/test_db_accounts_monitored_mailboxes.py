import os
import sqlite3
import tempfile
import unittest


class TestDbAccountsMonitoredMailboxes(unittest.TestCase):
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

    def test_new_db_accounts_has_imap_monitored_mailboxes_column(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(accounts)")
        columns = {row[1] for row in cur.fetchall()}
        conn.close()

        self.assertIn("imap_monitored_mailboxes", columns)
        self.assertIn("signature", columns)

    def test_legacy_db_is_migrated_and_preserves_rows(self):
        # Create a legacy accounts table without the new column.
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.executescript(
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
            """
        )
        cur.execute(
            """
            INSERT INTO accounts
              (email, password, imap_server, imap_port, imap_ssl,
               smtp_server, smtp_port, smtp_ssl, alias, tg_group_id)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a@example.com",
                "pw",
                "imap.example.com",
                993,
                1,
                "smtp.example.com",
                465,
                1,
                "Work",
                123,
            ),
        )
        conn.commit()
        conn.close()

        from app.database import DBManager

        DBManager.reset_instance()
        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()

        cur.execute("PRAGMA table_info(accounts)")
        columns = {row[1] for row in cur.fetchall()}
        self.assertIn("imap_monitored_mailboxes", columns)
        self.assertIn("signature", columns)

        cur.execute(
            "SELECT email, imap_monitored_mailboxes, signature FROM accounts WHERE email = ?",
            ("a@example.com",),
        )
        row = cur.fetchone()
        conn.close()

        self.assertEqual(row[0], "a@example.com")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
