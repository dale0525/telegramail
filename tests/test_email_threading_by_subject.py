import os
import tempfile
import unittest


class TestEmailThreadingBySubject(unittest.IsolatedAsyncioTestCase):
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

    async def test_get_thread_id_by_subject_prefers_latest_normalized_match(self):
        from app.database import DBManager
        from app.user.email_telegram import EmailTelegramSender

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails (email_account, subject, uid, mailbox, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "Invoice Payment Confirmation", "101", "INBOX", "101"),
        )
        cur.execute(
            """
            INSERT INTO emails (email_account, subject, uid, mailbox, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "Invoice Payment Confirmation - January", "102", "INBOX", "999"),
        )
        cur.execute(
            """
            INSERT INTO emails (email_account, subject, uid, mailbox, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "Re: Invoice Payment Confirmation", "103", "INBOX", "202"),
        )
        conn.commit()
        conn.close()

        sender = EmailTelegramSender.__new__(EmailTelegramSender)
        sender.db_manager = db

        thread_id = await sender.get_thread_id_by_subject(
            "Invoice Payment Confirmation", account_id=1
        )
        self.assertEqual(thread_id, 202)

    async def test_get_thread_id_by_subject_skips_deleted_topic(self):
        from app.database import DBManager
        from app.user.email_telegram import EmailTelegramSender

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails (email_account, subject, uid, mailbox, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "Invoice Payment Confirmation", "201", "INBOX", "201"),
        )
        cur.execute(
            """
            INSERT INTO emails (email_account, subject, uid, mailbox, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "Re: Invoice Payment Confirmation", "202", "INBOX", "303"),
        )
        cur.execute(
            """
            INSERT INTO deleted_topics (chat_id, thread_id, event_id, deleted_at)
            VALUES (?, ?, ?, ?)
            """,
            (777, "303", 1, 1),
        )
        conn.commit()
        conn.close()

        sender = EmailTelegramSender.__new__(EmailTelegramSender)
        sender.db_manager = db

        thread_id = await sender.get_thread_id_by_subject(
            "Invoice Payment Confirmation", account_id=1, chat_id=777
        )
        self.assertEqual(thread_id, 201)

