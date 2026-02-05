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


class TestDraftCallbacks(unittest.IsolatedAsyncioTestCase):
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

    async def test_draft_cancel_marks_draft_closed(self):
        from app.database import DBManager
        from app.bot.handlers.callback import callback_handler

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:cancel:{draft_id}"
        )

        await callback_handler(client, update)

        self.assertIsNone(db.get_active_draft(chat_id=123, thread_id=456))

    async def test_draft_send_marks_sent_and_calls_smtp(self):
        from app.database import DBManager
        from app.bot.handlers.callback import callback_handler

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
                "body_markdown": "Hi",
            },
        )

        called = {}

        class _FakeSMTPClient:
            def __init__(self, **kwargs):
                called["init"] = kwargs

            def send_email_sync(self, **kwargs):
                called["send"] = kwargs
                return True

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:send:{draft_id}"
        )

        # Patch the SMTPClient used by the handler
        from unittest import mock

        with mock.patch("app.bot.handlers.callbacks.drafts.SMTPClient", _FakeSMTPClient):
            await callback_handler(client, update)

        self.assertIn("send", called)
        self.assertEqual(called["send"]["from_email"], "a@example.com")
        self.assertEqual(called["send"]["to_addrs"], ["to@example.com"])
        self.assertEqual(called["send"]["subject"], "Hello")
        self.assertEqual(called["send"]["text_body"], "Hi")
        self.assertIsNotNone(called["send"]["html_body"])
        self.assertIn("<p>Hi</p>", called["send"]["html_body"])

        # Draft should no longer be active
        self.assertIsNone(db.get_active_draft(chat_id=123, thread_id=456))

    async def test_draft_send_passes_reply_headers_when_present(self):
        from app.database import DBManager
        from app.bot.handlers.callback import callback_handler

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="reply",
            from_identity_email="a@example.com",
        )
        db.update_draft(
            draft_id=draft_id,
            updates={
                "to_addrs": "to@example.com",
                "subject": "Re: Hello",
                "body_markdown": "Hi",
                "in_reply_to": "<m1@example.com>",
                "references_header": "<r1@example.com> <r2@example.com>",
            },
        )

        called = {}

        class _FakeSMTPClient:
            def __init__(self, **kwargs):
                called["init"] = kwargs

            def send_email_sync(self, **kwargs):
                called["send"] = kwargs
                return True

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:send:{draft_id}"
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.callbacks.drafts.SMTPClient", _FakeSMTPClient):
            await callback_handler(client, update)

        self.assertEqual(called["send"]["in_reply_to"], "<m1@example.com>")
        self.assertEqual(
            called["send"]["references"], ["<r1@example.com>", "<r2@example.com>"]
        )

    async def test_draft_send_uses_identity_display_name(self):
        from app.database import DBManager
        from app.bot.handlers.callback import callback_handler

        db = DBManager()
        db.upsert_account_identity(
            account_id=self.account["id"],
            from_email="a@example.com",
            display_name="Primary Identity",
            is_default=True,
        )

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
                "body_markdown": "Hi",
            },
        )

        called = {}

        class _FakeSMTPClient:
            def __init__(self, **kwargs):
                called["init"] = kwargs

            def send_email_sync(self, **kwargs):
                called["send"] = kwargs
                return True

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:send:{draft_id}"
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.callbacks.drafts.SMTPClient", _FakeSMTPClient):
            await callback_handler(client, update)

        self.assertEqual(called["send"]["from_name"], "Primary Identity")
