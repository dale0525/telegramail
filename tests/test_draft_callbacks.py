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
        self.deleted_forum_topics = []
        self.deleted_messages = []
        self.edited_forum_topics = []
        self.sent_messages = []

    async def answer_callback_query(self, callback_query_id: int, text: str, url: str, cache_time: int):
        return None

    async def delete_forum_topic(self, *, chat_id: int, message_thread_id: int):
        self.deleted_forum_topics.append(
            {"chat_id": int(chat_id), "message_thread_id": int(message_thread_id)}
        )

    async def delete_messages(self, *, chat_id: int, message_ids: list[int], revoke: bool):
        self.deleted_messages.append(
            {"chat_id": int(chat_id), "message_ids": list(message_ids), "revoke": bool(revoke)}
        )

    async def edit_forum_topic(self, **kwargs):
        self.edited_forum_topics.append(kwargs)

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return type("Msg", (), {"id": 999})


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

    async def test_draft_cancel_deletes_compose_topic(self):
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

        self.assertEqual(
            client.api.deleted_forum_topics,
            [{"chat_id": 123, "message_thread_id": 456}],
        )

    async def test_draft_cancel_does_not_delete_topic_for_reply_draft(self):
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

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:cancel:{draft_id}"
        )

        await callback_handler(client, update)

        self.assertEqual(client.api.deleted_forum_topics, [])

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
        self.assertIn("message_id", called["send"])
        self.assertTrue(str(called["send"]["message_id"]).startswith("<"))
        self.assertTrue(str(called["send"]["message_id"]).endswith(">"))
        self.assertEqual(called["send"]["from_email"], "a@example.com")
        self.assertEqual(called["send"]["to_addrs"], ["to@example.com"])
        self.assertEqual(called["send"]["subject"], "Hello")
        self.assertEqual(called["send"]["text_body"], "Hi")
        self.assertIsNotNone(called["send"]["html_body"])
        self.assertIn("<p>Hi</p>", called["send"]["html_body"])

        # Draft should no longer be active
        self.assertIsNone(db.get_active_draft(chat_id=123, thread_id=456))

    async def test_draft_send_persists_outgoing_email_record(self):
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

        class _FakeSMTPClient:
            def __init__(self, **kwargs):
                pass

            def send_email_sync(self, **kwargs):
                return True

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:send:{draft_id}"
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.callbacks.drafts.SMTPClient", _FakeSMTPClient):
            await callback_handler(client, update)

        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message_id, uid, telegram_thread_id, subject
            FROM emails
            WHERE email_account = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (self.account["id"],),
        )
        row = cur.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        msg_id, uid, thread_id, subject = row
        self.assertTrue(str(msg_id).startswith("<") and str(msg_id).endswith(">"))
        self.assertTrue(str(uid).startswith("outgoing:"))
        self.assertEqual(str(thread_id), "456")
        self.assertEqual(subject, "Hello")

    async def test_draft_send_deletes_compose_topic_after_success(self):
        import asyncio
        from app.database import DBManager
        from app.bot.handlers.callback import callback_handler

        os.environ["TELEGRAMAIL_COMPOSE_DRAFT_DELETE_DELAY_SECONDS"] = "0"
        try:
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

            class _FakeSMTPClient:
                def __init__(self, **kwargs):
                    pass

                def send_email_sync(self, **kwargs):
                    return True

            client = _FakeClient()
            update = _FakeCallbackUpdate(
                chat_id=123, user_id=1, message_id=10, data=f"draft:send:{draft_id}"
            )

            from unittest import mock

            with mock.patch("app.bot.handlers.callbacks.drafts.SMTPClient", _FakeSMTPClient):
                await callback_handler(client, update)

            # Allow any scheduled deletion task to run.
            await asyncio.sleep(0)

            self.assertEqual(client.api.deleted_forum_topics, [])
        finally:
            os.environ.pop("TELEGRAMAIL_COMPOSE_DRAFT_DELETE_DELAY_SECONDS", None)

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

    async def test_draft_send_deletes_card_and_tracked_messages(self):
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
            },
        )

        # Simulate user typing 2 body messages that should be cleaned up after send.
        conn = db._get_connection()
        cur = conn.cursor()
        cur.executemany(
            """
            INSERT INTO draft_messages (draft_id, chat_id, thread_id, message_id, message_type, created_at)
            VALUES (?, ?, ?, ?, ?, strftime('%s','now'))
            """,
            [
                (draft_id, 123, 456, 11, "text"),
                (draft_id, 123, 456, 12, "text"),
            ],
        )
        conn.commit()
        conn.close()

        class _FakeSMTPClient:
            def __init__(self, **kwargs):
                pass

            def send_email_sync(self, **kwargs):
                return True

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:send:{draft_id}"
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.callbacks.drafts.SMTPClient", _FakeSMTPClient):
            await callback_handler(client, update)

        self.assertEqual(
            client.api.deleted_messages,
            [{"chat_id": 123, "message_ids": [10, 11, 12], "revoke": True}],
        )
