import os
import tempfile
import time
import unittest


class TestDbEmailLabels(unittest.TestCase):
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

    def _create_account(self):
        from app.email_utils.account_manager import AccountManager

        account_mgr = AccountManager()
        ok = account_mgr.add_account(
            {
                "email": "a@example.com",
                "password": "pw",
                "imap_server": "imap.example.com",
                "imap_port": 993,
                "imap_ssl": True,
                "smtp_server": "smtp.example.com",
                "smtp_port": 465,
                "smtp_ssl": True,
                "alias": "Work",
                "tg_group_id": 123,
            }
        )
        self.assertTrue(ok)
        account = account_mgr.get_account(
            email="a@example.com", smtp_server="smtp.example.com"
        )
        self.assertIsNotNone(account)
        return account

    def test_new_db_has_llm_label_columns(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(emails)")
        columns = {row[1] for row in cur.fetchall()}
        conn.close()

        self.assertIn("llm_category", columns)
        self.assertIn("llm_priority", columns)
        self.assertIn("llm_confidence", columns)
        self.assertIn("llm_labeled_at", columns)

    def test_can_query_labeled_emails_by_category_and_time(self):
        from app.database import DBManager

        account = self._create_account()
        db = DBManager()
        now = int(time.time())
        old = now - 40 * 24 * 3600

        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails
              (email_account, message_id, sender, subject, uid, mailbox, telegram_thread_id,
               llm_category, llm_priority, llm_confidence, llm_labeled_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account["id"],
                "<m1@example.com>",
                "Alice <alice@example.com>",
                "Task recent",
                "u1",
                "INBOX",
                "456",
                "task",
                "high",
                0.91,
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO emails
              (email_account, message_id, sender, subject, uid, mailbox, telegram_thread_id,
               llm_category, llm_priority, llm_confidence, llm_labeled_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account["id"],
                "<m2@example.com>",
                "Bob <bob@example.com>",
                "Meeting recent",
                "u2",
                "INBOX",
                "457",
                "meeting",
                "medium",
                0.74,
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO emails
              (email_account, message_id, sender, subject, uid, mailbox, telegram_thread_id,
               llm_category, llm_priority, llm_confidence, llm_labeled_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account["id"],
                "<m3@example.com>",
                "Carol <carol@example.com>",
                "Task old",
                "u3",
                "INBOX",
                "458",
                "task",
                "low",
                0.62,
                old,
            ),
        )
        conn.commit()
        conn.close()

        rows = db.list_labeled_emails(
            category="task",
            days=7,
            account_ids=[account["id"]],
            limit=10,
            offset=0,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Task recent")
        self.assertEqual(rows[0]["telegram_thread_id"], "456")

        stats = db.count_labeled_emails_by_category(days=30, account_ids=[account["id"]])
        self.assertEqual(stats.get("task"), 1)
        self.assertEqual(stats.get("meeting"), 1)
        self.assertEqual(stats.get("newsletter", 0), 0)
