import os
import tempfile
import unittest


class _FakeCallbackPayload:
    def __init__(self, data: bytes):
        self.data = data


class _FakeCallbackUpdate:
    def __init__(self, *, chat_id: int, user_id: int, message_id: int, data: str):
        self.chat_id = chat_id
        self.sender_user_id = user_id
        self.message_id = message_id
        self.payload = _FakeCallbackPayload(data=data.encode("utf-8"))
        self.id = 1


class _FakeApi:
    def __init__(self):
        self.answered = []

    async def answer_callback_query(self, callback_query_id: int, text: str, url: str, cache_time: int):
        self.answered.append((callback_query_id, text))


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()
        self.edits = []

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestIdentitySuggestionCallback(unittest.IsolatedAsyncioTestCase):
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

    async def test_callback_add_identity_creates_identity_and_marks_accepted(self):
        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager
        from app.bot.handlers.callback import callback_handler

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
        suggestion = db.upsert_identity_suggestion(
            account_id=account_id,
            suggested_email="b@example.com",
            source_delivered_to="b+tag@example.com",
            email_id=99,
        )

        client = _FakeClient()
        update = _FakeCallbackUpdate(chat_id=123, user_id=1, message_id=10, data=f"id_suggest:add:{suggestion['id']}")

        await callback_handler(client, update)

        identities = db.list_account_identities(account_id=account_id)
        self.assertTrue(any(i["from_email"] == "b@example.com" and i["display_name"] == "Work" for i in identities))

        s = db.get_identity_suggestion(suggestion["id"])
        self.assertEqual(s["status"], "accepted")

    async def test_callback_ignore_identity_marks_ignored(self):
        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager
        from app.bot.handlers.callback import callback_handler

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
        suggestion = db.upsert_identity_suggestion(
            account_id=account_id,
            suggested_email="b@example.com",
            source_delivered_to="b+tag@example.com",
            email_id=99,
        )

        client = _FakeClient()
        update = _FakeCallbackUpdate(chat_id=123, user_id=1, message_id=10, data=f"id_suggest:ignore:{suggestion['id']}")

        await callback_handler(client, update)

        s = db.get_identity_suggestion(suggestion["id"])
        self.assertEqual(s["status"], "ignored")

