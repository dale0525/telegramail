import os
import tempfile
import unittest


class TestDbIdentitySchema(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "telegramail-test.db")
        os.environ["TELEGRAMAIL_DB_PATH"] = self.db_path

        # Reset singletons between tests (added by our implementation).
        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager

        if hasattr(DBManager, "reset_instance"):
            DBManager.reset_instance()
        if hasattr(AccountManager, "reset_instance"):
            AccountManager.reset_instance()

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    def test_add_account_creates_default_identity(self):
        from app.email_utils.account_manager import AccountManager
        from app.database import DBManager

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

        account = account_mgr.get_account(email="a@example.com", smtp_server="smtp.example.com")
        self.assertIsNotNone(account)
        account_id = account["id"]

        db = DBManager()
        identities = db.list_account_identities(account_id=account_id)
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["from_email"], "a@example.com")
        self.assertEqual(identities[0]["display_name"], "Work")
        self.assertEqual(int(identities[0]["is_default"]), 1)

    def test_identity_suggestion_upsert_and_ignore(self):
        from app.email_utils.account_manager import AccountManager
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
        account_id = account["id"]

        db = DBManager()
        s1 = db.upsert_identity_suggestion(
            account_id=account_id,
            suggested_email="b@example.com",
            source_delivered_to="b+tag@example.com",
            email_id=99,
        )
        s2 = db.upsert_identity_suggestion(
            account_id=account_id,
            suggested_email="b@example.com",
            source_delivered_to="b+tag@example.com",
            email_id=99,
        )

        self.assertEqual(s1["id"], s2["id"])
        suggestion = db.get_identity_suggestion(s1["id"])
        self.assertEqual(suggestion["suggested_email"], "b@example.com")
        self.assertEqual(suggestion["status"], "pending")

        db.mark_identity_suggestion_ignored(suggestion_id=s1["id"])
        suggestion = db.get_identity_suggestion(s1["id"])
        self.assertEqual(suggestion["status"], "ignored")

        # Once ignored, upsert should keep ignored (no re-prompt).
        s3 = db.upsert_identity_suggestion(
            account_id=account_id,
            suggested_email="b@example.com",
            source_delivered_to="b+tag@example.com",
            email_id=100,
        )
        self.assertEqual(s3["status"], "ignored")

