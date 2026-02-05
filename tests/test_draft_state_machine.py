import os
import tempfile
import unittest


class TestDraftStateMachine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "telegramail-test.db")
        os.environ["TELEGRAMAIL_DB_PATH"] = self.db_path

        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager

        DBManager.reset_instance()
        AccountManager.reset_instance()

        from app.email_utils.account_manager import AccountManager as AM

        self.account_mgr = AM()
        self.assertTrue(
            self.account_mgr.add_account(
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
        self.account = self.account_mgr.get_account(
            email="a@example.com", smtp_server="smtp.example.com"
        )

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    def test_create_and_get_active_draft(self):
        from app.database import DBManager

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        self.assertIsInstance(draft_id, int)

        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["id"], draft_id)
        self.assertEqual(draft["status"], "open")

    def test_append_body_adds_blank_line_between_messages(self):
        from app.database import DBManager

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )

        db.append_draft_body(draft_id=draft_id, text="Hello")
        db.append_draft_body(draft_id=draft_id, text="World")

        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertEqual(draft["body_markdown"], "Hello\n\nWorld")

    def test_update_draft_fields(self):
        from app.database import DBManager

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )

        db.update_draft(
            draft_id=draft_id,
            updates={
                "to_addrs": "to@example.com",
                "subject": "Hello",
            },
        )

        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertEqual(draft["to_addrs"], "to@example.com")
        self.assertEqual(draft["subject"], "Hello")

