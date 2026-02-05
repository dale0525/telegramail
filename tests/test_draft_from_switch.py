import os
import tempfile
import unittest


class _FakeSenderId:
    def __init__(self, user_id: int):
        self.user_id = user_id


class _FakeMessageText:
    ID = "messageText"

    def __init__(self, text: str):
        self.text = type("_T", (), {"text": text})()


class _FakeMessage:
    def __init__(self, *, chat_id: int, thread_id: int, user_id: int, text: str):
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.sender_id = _FakeSenderId(user_id)
        self.content = _FakeMessageText(text)


class _FakeUpdate:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeClient:
    def __init__(self):
        class _Api:
            def __init__(self, outer):
                self._outer = outer

            async def send_message(self, **kwargs):
                self._outer.sent_messages.append(kwargs)

        self.edits = []
        self.sent_messages = []
        self.api = _Api(self)

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestDraftFromSwitch(unittest.IsolatedAsyncioTestCase):
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

    async def test_message_handler_from_command_updates_draft_identity(self):
        from app.database import DBManager

        db = DBManager()
        # Add an extra identity the user can switch to.
        db.upsert_account_identity(
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
        update = _FakeUpdate(
            _FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/from b@example.com")
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["from_identity_email"], "b@example.com")

        self.assertTrue(client.edits)
        last_text = client.edits[-1].get("text") or ""
        self.assertIn("From: b@example.com", last_text)

    async def test_message_handler_from_with_bot_mention_opens_selector(self):
        from app.database import DBManager

        db = DBManager()
        db.upsert_account_identity(
            account_id=self.account["id"],
            from_email="a@example.com",
            display_name="Work",
            is_default=True,
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
        update = _FakeUpdate(
            _FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/from@LogicEmailBot")
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        self.assertTrue(client.sent_messages)
        self.assertFalse(client.edits)

        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertIsNotNone(draft)
        self.assertEqual((draft.get("body_markdown") or "").strip(), "")

    async def test_message_handler_from_creates_default_identity_when_missing(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM account_identities WHERE account_id = ?", (int(self.account["id"]),))
        conn.commit()
        conn.close()

        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(draft_id=draft_id, updates={"card_message_id": 99})

        client = _FakeClient()
        update = _FakeUpdate(_FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/from"))

        from unittest import mock

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        self.assertTrue(client.sent_messages)
        identities = db.list_account_identities(account_id=self.account["id"])
        self.assertTrue(any(i.get("from_email") == "a@example.com" for i in identities))

    async def test_message_handler_to_without_arg_sends_help_instead_of_appending_body(self):
        from app.database import DBManager

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(draft_id=draft_id, updates={"card_message_id": 99})

        client = _FakeClient()
        update = _FakeUpdate(
            _FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/to@LogicEmailBot")
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        self.assertTrue(client.sent_messages)
        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertIsNotNone(draft)
        self.assertEqual((draft.get("body_markdown") or "").strip(), "")
