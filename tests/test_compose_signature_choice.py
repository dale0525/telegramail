import os
import tempfile
import unittest


class _FakeSenderId:
    def __init__(self, user_id: int):
        self.user_id = user_id


class _FakeMessage:
    def __init__(self, *, chat_id: int, user_id: int):
        self.chat_id = chat_id
        self.sender_id = _FakeSenderId(user_id)


class _FakeUpdate:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeApi:
    def __init__(self):
        self.sent_messages = []

    async def create_forum_topic(self, **kwargs):
        return type("_Topic", (), {"message_thread_id": 777})()

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return type("_Msg", (), {"id": 9001})()

    async def pin_chat_message(self, **kwargs):
        return None


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()

    async def send_text(self, *args, **kwargs):
        raise AssertionError("send_text should not be called in this test")


class TestComposeSignatureChoice(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "telegramail-test.db")
        os.environ["TELEGRAMAIL_DB_PATH"] = self.db_path

        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager

        DBManager.reset_instance()
        AccountManager.reset_instance()

        from app.email_utils.account_manager import AccountManager as AM
        from app.email_utils.signatures import add_account_signature

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
            email="a@example.com",
            smtp_server="smtp.example.com",
        )

        raw = None
        raw, self.default_id = add_account_signature(
            raw,
            name="Default",
            markdown="Default signature",
        )
        raw, self.alt_id = add_account_signature(
            raw,
            name="Alt",
            markdown="Alt signature",
        )
        self.account_mgr.update_account(
            id=self.account["id"],
            updates={"signature": raw},
        )

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    async def test_compose_uses_last_signature_choice_for_new_draft(self):
        from unittest import mock

        from app.bot.handlers.compose import compose_command_handler
        from app.database import DBManager
        from app.email_utils.signatures import (
            get_draft_signature_choice,
            set_account_last_signature_choice,
        )

        set_account_last_signature_choice(
            account_id=int(self.account["id"]),
            choice=self.alt_id,
        )

        client = _FakeClient()
        update = _FakeUpdate(_FakeMessage(chat_id=123, user_id=1))

        with mock.patch("app.bot.handlers.compose.validate_admin", lambda _u: True):
            await compose_command_handler(client, update)

        db = DBManager()
        draft = db.get_active_draft(chat_id=123, thread_id=777)
        self.assertIsNotNone(draft)
        self.assertEqual(
            get_draft_signature_choice(draft_id=int(draft["id"])),
            self.alt_id,
        )

        self.assertTrue(client.api.sent_messages)
        text = (
            getattr(
                client.api.sent_messages[-1]["input_message_content"].text,
                "text",
                "",
            )
            or ""
        )
        self.assertIn("Alt", text)

