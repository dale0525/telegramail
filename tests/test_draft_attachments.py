import os
import tempfile
import unittest


class TestDraftAttachments(unittest.TestCase):
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

    def test_add_and_list_draft_attachments(self):
        from app.database import DBManager

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )

        att_id = db.add_draft_attachment(
            draft_id=draft_id,
            file_id=42,
            remote_id="remote42",
            file_type="document",
            file_name="a.txt",
            mime_type="text/plain",
            size=3,
        )
        self.assertIsInstance(att_id, int)

        attachments = db.list_draft_attachments(draft_id=draft_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["id"], att_id)
        self.assertEqual(attachments[0]["file_name"], "a.txt")

