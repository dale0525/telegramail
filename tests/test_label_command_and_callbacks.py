import os
import tempfile
import time
import unittest
from unittest import mock


class _FakeSenderId:
    def __init__(self, user_id: int):
        self.user_id = user_id


class _FakeFormattedText:
    def __init__(self, text: str):
        self.text = text


class _FakeMessageText:
    ID = "messageText"

    def __init__(self, text: str):
        self.text = _FakeFormattedText(text)


class _FakeMessage:
    def __init__(self, *, chat_id: int, user_id: int, text: str):
        self.chat_id = chat_id
        self.sender_id = _FakeSenderId(user_id)
        self.content = _FakeMessageText(text)


class _FakeCommandUpdate:
    def __init__(self, *, chat_id: int, user_id: int, text: str):
        self.message = _FakeMessage(chat_id=chat_id, user_id=user_id, text=text)


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
        self.answered = []

    async def answer_callback_query(
        self, callback_query_id: int, text: str, url: str, cache_time: int
    ):
        self.answered.append((callback_query_id, text))

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return type("Msg", (), {"id": 999})


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()
        self.edits = []

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestLabelCommandAndCallbacks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "telegramail-test.db")
        os.environ["TELEGRAMAIL_DB_PATH"] = self.db_path

        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager

        DBManager.reset_instance()
        AccountManager.reset_instance()

        self.account = self._create_account()
        self.email_id = self._insert_labeled_email(
            category="task", priority="high", thread_id=456
        )

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    def _create_account(self):
        from app.email_utils.account_manager import AccountManager

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
        account = account_mgr.get_account(
            email="a@example.com", smtp_server="smtp.example.com"
        )
        self.assertIsNotNone(account)
        return account

    def _insert_labeled_email(self, *, category: str, priority: str, thread_id: int) -> int:
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails
              (email_account, message_id, sender, subject, email_date, uid, mailbox, telegram_thread_id,
               llm_category, llm_priority, llm_confidence, llm_labeled_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.account["id"],
                "<m1@example.com>",
                "Alice <alice@example.com>",
                "Need action",
                "2026-02-06",
                "u1",
                "INBOX",
                str(thread_id),
                category,
                priority,
                0.93,
                int(time.time()),
            ),
        )
        email_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return email_id

    def _collect_callback_data(self, markup) -> list[str]:
        all_callback_data: list[str] = []
        for row in getattr(markup, "rows", []) or []:
            for button in row:
                button_type = getattr(button, "type_", None) or getattr(
                    button, "type", None
                )
                payload = getattr(button_type, "data", b"")
                if isinstance(payload, (bytes, bytearray)):
                    all_callback_data.append(payload.decode("utf-8"))
        return all_callback_data

    async def test_label_command_without_args_renders_panel(self):
        from app.bot.handlers.labels import label_command_handler

        client = _FakeClient()
        update = _FakeCommandUpdate(chat_id=123, user_id=1, text="/label")

        with mock.patch("app.bot.handlers.labels.validate_admin", lambda _u: True):
            await label_command_handler(client, update)

        self.assertTrue(client.api.sent)
        markup = client.api.sent[-1]["reply_markup"]
        all_callback_data = self._collect_callback_data(markup)
        self.assertTrue(any(d.startswith("label:list:task:7:0") for d in all_callback_data))

    async def test_label_command_accepts_chinese_category_alias(self):
        from app.bot.handlers.labels import label_command_handler

        client = _FakeClient()
        update = _FakeCommandUpdate(chat_id=123, user_id=1, text="/label 任务 7天")

        with mock.patch("app.bot.handlers.labels.validate_admin", lambda _u: True):
            await label_command_handler(client, update)

        self.assertTrue(client.api.sent)
        markup = client.api.sent[-1]["reply_markup"]
        all_callback_data = self._collect_callback_data(markup)
        self.assertIn(f"email:reply:{self.email_id}:456", all_callback_data)

    async def test_label_command_accepts_chinese_stats_alias(self):
        from app.bot.handlers.labels import label_command_handler

        client = _FakeClient()
        update = _FakeCommandUpdate(chat_id=123, user_id=1, text="/label 统计 30天")

        with mock.patch("app.bot.handlers.labels.validate_admin", lambda _u: True):
            await label_command_handler(client, update)

        self.assertTrue(client.api.sent)
        markup = client.api.sent[-1]["reply_markup"]
        all_callback_data = self._collect_callback_data(markup)
        self.assertIn("label:list:task:30:0", all_callback_data)

    async def test_label_list_callback_renders_results_with_action_buttons(self):
        from app.bot.handlers.callback import callback_handler

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=10,
            data="label:list:task:7:0",
        )

        await callback_handler(client, update)

        self.assertTrue(client.edits)
        markup = client.edits[-1]["reply_markup"]
        all_callback_data = self._collect_callback_data(markup)

        self.assertIn(f"email:reply:{self.email_id}:456", all_callback_data)
        self.assertIn(f"email:forward:{self.email_id}:456", all_callback_data)
        self.assertIn("label:locate:456", all_callback_data)

    async def test_label_locate_callback_posts_anchor_message_to_topic(self):
        from app.bot.handlers.callback import callback_handler

        client = _FakeClient()
        update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=10,
            data="label:locate:456",
        )

        await callback_handler(client, update)

        self.assertTrue(client.api.sent)
        self.assertEqual(client.api.sent[-1]["chat_id"], 123)
        self.assertEqual(client.api.sent[-1]["message_thread_id"], 456)
