import os
import tempfile
import unittest


class _FakeSenderId:
    def __init__(self, user_id: int):
        self.user_id = user_id


class _FakeMessage:
    def __init__(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str | None = None,
        message_id: int = 1,
    ):
        self.chat_id = chat_id
        self.sender_id = _FakeSenderId(user_id)
        self.id = message_id
        if text is not None:
            self.content = type(
                "_TextContent",
                (),
                {"ID": "messageText", "text": type("_T", (), {"text": text})()},
            )()


class _FakeUpdate:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeApi:
    def __init__(self):
        self.sent_messages = []
        self.deleted_messages = []

    async def create_forum_topic(self, **kwargs):
        return type("_Topic", (), {"message_thread_id": 777})()

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return type("_Msg", (), {"id": 9001})()

    async def pin_chat_message(self, **kwargs):
        return None

    async def delete_messages(self, *, chat_id: int, message_ids: list[int], revoke: bool):
        self.deleted_messages.append(
            {"chat_id": int(chat_id), "message_ids": list(message_ids), "revoke": bool(revoke)}
        )


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()
        self.sent_texts = []
        self.edits = []
        self._next_message_id = 100

    async def send_text(self, chat_id: int, text: str, **kwargs):
        self._next_message_id += 1
        self.sent_texts.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return type("_Msg", (), {"id": self._next_message_id})()

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


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
        from app.bot.conversation import Conversation

        # Conversation instances are process-global; clear to avoid leaking state
        # into unrelated callback/message tests that reuse chat/user ids.
        Conversation._instances.clear()
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

    async def test_compose_starts_interactive_conversation_and_updates_draft(self):
        from unittest import mock

        from app.bot.conversation import Conversation, ConversationState
        from app.bot.handlers.compose import compose_command_handler
        from app.database import DBManager

        client = _FakeClient()
        update = _FakeUpdate(_FakeMessage(chat_id=123, user_id=1))

        with mock.patch("app.bot.handlers.compose.validate_admin", lambda _u: True):
            await compose_command_handler(client, update)

        conversation = Conversation.get_instance(123, 1)
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.state, ConversationState.ACTIVE)
        self.assertTrue(client.sent_texts)

        for idx, value in enumerate(
            ["to@example.com", "/skip", "/skip", "Hello Subject", "Hello Body"],
            start=1,
        ):
            handled = await conversation.handle_update(
                _FakeUpdate(
                    _FakeMessage(
                        chat_id=123,
                        user_id=1,
                        text=value,
                        message_id=200 + idx,
                    )
                )
            )
            self.assertTrue(handled)

        self.assertIsNone(Conversation.get_instance(123, 1))
        db = DBManager()
        draft = db.get_active_draft(chat_id=123, thread_id=777)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["to_addrs"], "to@example.com")
        self.assertEqual(draft["subject"], "Hello Subject")
        self.assertEqual(draft["body_markdown"], "Hello Body")
