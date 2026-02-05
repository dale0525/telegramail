import json
import os
import tempfile
import unittest


class TestImapDeliveredToStore(unittest.TestCase):
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

    def test_execute_db_transaction_persists_delivered_to_json(self):
        from app.email_utils.account_manager import AccountManager
        from app.email_utils.imap_client import IMAPClient
        from app.database import DBManager

        account_mgr = AccountManager()
        self.assertTrue(
            account_mgr.add_account(
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
        )
        account = account_mgr.get_account(email="a@example.com", smtp_server="smtp.example.com")

        imap = IMAPClient(account)
        delivered_to = ["b+tag@example.com", "b@example.com"]

        email_data = {
            "email_account": account["id"],
            "message_id": "<m1@example.com>",
            "sender": "Alice <alice@example.com>",
            "recipient": "a@example.com",
            "cc": "",
            "bcc": "",
            "subject": "Hello",
            "email_date": "Mon, 1 Jan 2026 00:00:00 +0000",
            "body_text": "Hi",
            "body_html": "<p>Hi</p>",
            "uid": "100",
            "delivered_to": json.dumps(delivered_to),
        }

        email_db_id, is_new = imap._execute_db_transaction(email_data, email_data["uid"])
        self.assertTrue(is_new)

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT delivered_to FROM emails WHERE id = ?", (email_db_id,))
        row = cur.fetchone()
        conn.close()

        self.assertEqual(json.loads(row[0]), delivered_to)

