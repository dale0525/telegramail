import os
import tempfile
import unittest


class TestEmailThreadingByHeaders(unittest.TestCase):
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

    def test_find_thread_id_for_in_reply_to(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<m1@example.com>", "Hello", "outgoing:<m1@example.com>", "456"),
        )
        conn.commit()
        conn.close()

        thread_id = db.find_thread_id_for_reply_headers(
            account_id=1, in_reply_to="<m1@example.com>", references_header=None
        )
        self.assertEqual(thread_id, 456)

    def test_find_thread_id_for_references_header(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<m2@example.com>", "Hello", "u2", "789"),
        )
        conn.commit()
        conn.close()

        thread_id = db.find_thread_id_for_reply_headers(
            account_id=1,
            in_reply_to=None,
            references_header="<x@example.com> <m2@example.com>",
        )
        self.assertEqual(thread_id, 789)

    def test_get_email_uid_by_thread_filters_non_numeric_uids(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        # Outgoing / synthetic UID should not be returned for deletion.
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<m1@example.com>", "Hello", "outgoing:<m1@example.com>", "456"),
        )
        cur.execute(
            """
            INSERT INTO emails (email_account, message_id, subject, uid, telegram_thread_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "<m3@example.com>", "Re: Hello", "42", "456"),
        )
        conn.commit()
        conn.close()

        account_id, uids = db.get_email_uid_by_telegram_thread_id("456")
        self.assertEqual(account_id, 1)
        self.assertEqual(uids, ["42"])

