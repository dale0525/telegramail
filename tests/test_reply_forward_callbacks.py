import json
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
        self.sent = []

    async def answer_callback_query(self, callback_query_id: int, text: str, url: str, cache_time: int):
        return None

    async def send_message(self, **kwargs):
        # Record call and return object with id
        self.sent.append(kwargs)
        return type("Msg", (), {"id": 999})


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()
        self.edits = []

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestReplyForwardCallbacks(unittest.IsolatedAsyncioTestCase):
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

        # Add an alias identity b@example.com so plus-address can map to base.
        from app.database import DBManager as DB

        DB().upsert_account_identity(
            account_id=self.account["id"],
            from_email="b@example.com",
            display_name="Work",
            is_default=False,
        )

        # Insert an email row with delivered_to = b+tag@example.com
        db = DB()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails
              (email_account, message_id, sender, recipient, cc, bcc, subject, email_date, body_text, body_html, uid, delivered_to)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.account["id"],
                "<m1@example.com>",
                "Alice <alice@example.com>",
                "a@example.com",
                "",
                "",
                "Hello",
                "Mon, 1 Jan 2026 00:00:00 +0000",
                "Hi",
                "<p>Hi</p>",
                "u1",
                json.dumps(["b+tag@example.com"]),
            ),
        )
        self.email_id = cur.lastrowid
        conn.commit()
        conn.close()

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    async def test_reply_callback_creates_draft_with_recommended_from_identity(self):
        from app.bot.handlers.callback import callback_handler
        from app.database import DBManager

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=10,
            data=f"email:reply:{self.email_id}:456",
        )

        await callback_handler(client, update)

        draft = DBManager().get_active_draft(chat_id=123, thread_id=456)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["from_identity_email"], "b@example.com")
        self.assertEqual(draft["to_addrs"], "alice@example.com")
        self.assertTrue(draft["subject"].startswith("Re:"))
        self.assertEqual(draft.get("in_reply_to"), "<m1@example.com>")
        self.assertEqual(draft.get("references_header"), "<m1@example.com>")

    async def test_forward_callback_creates_draft_with_forward_header_and_recommended_identity(
        self,
    ):
        from app.bot.handlers.callback import callback_handler
        from app.database import DBManager

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=10,
            data=f"email:forward:{self.email_id}:456",
        )

        await callback_handler(client, update)

        draft = DBManager().get_active_draft(chat_id=123, thread_id=456)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["from_identity_email"], "b@example.com")
        self.assertEqual(draft["to_addrs"], "")
        self.assertTrue(draft["subject"].lower().startswith("fwd:"))

        body = draft.get("body_markdown") or ""
        self.assertIn("---------- Forwarded message ----------", body)
        self.assertIn("From: Alice <alice@example.com>", body)
        self.assertIn("Date: Mon, 1 Jan 2026 00:00:00 +0000", body)
        self.assertIn("Subject: Hello", body)
        self.assertIn("To: a@example.com", body)
        self.assertNotIn("Cc:", body)
        self.assertIn("> Hi", body)
