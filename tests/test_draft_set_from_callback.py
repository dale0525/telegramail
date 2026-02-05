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
    async def answer_callback_query(self, callback_query_id: int, text: str, url: str, cache_time: int):
        return None


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()
        self.edits = []

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestDraftSetFromCallback(unittest.IsolatedAsyncioTestCase):
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

    async def test_callback_sets_from_identity_and_updates_card(self):
        from app.database import DBManager
        from app.bot.handlers.callback import callback_handler

        db = DBManager()
        identity_id = db.upsert_account_identity(
            account_id=self.account["id"],
            from_email="b@example.com",
            display_name="Work",
            is_default=False,
        )

        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(draft_id=draft_id, updates={"card_message_id": 99})

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=77,
            data=f"draft:set_from:{draft_id}:{identity_id}",
        )

        await callback_handler(client, update)

        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["from_identity_email"], "b@example.com")

        # The draft card should be refreshed.
        edited_card = next((e for e in client.edits if int(e.get("message_id") or 0) == 99), None)
        self.assertIsNotNone(edited_card)
        self.assertIn("From: b@example.com", edited_card.get("text") or "")

