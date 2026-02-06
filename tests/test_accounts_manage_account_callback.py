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

    async def answer_callback_query(
        self, callback_query_id: int, text: str, url: str, cache_time: int
    ):
        self.answered.append((callback_query_id, text))


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()
        self.edits = []

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestManageAccountCallback(unittest.IsolatedAsyncioTestCase):
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

    async def test_manage_account_callback_renders_menu(self):
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
        account_id = str(account["id"])

        client = _FakeClient()
        await callback_handler(
            client,
            _FakeCallbackUpdate(
                chat_id=123,
                user_id=1,
                message_id=10,
                data=f"manage_account:{account_id}",
            ),
        )

        self.assertTrue(client.edits)
        reply_markup = client.edits[-1].get("reply_markup")
        self.assertIsNotNone(reply_markup)
        callback_data_values = []
        for row in getattr(reply_markup, "rows", []):
            for button in row:
                button_type = getattr(button, "type_", None) or getattr(button, "type", None)
                data = getattr(button_type, "data", None)
                if isinstance(data, (bytes, bytearray)):
                    callback_data_values.append(data.decode("utf-8"))

        self.assertIn(
            f"account_signature:{account_id}",
            callback_data_values,
        )

    async def test_account_signature_clear_callback_updates_account(self):
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
        account = account_mgr.get_account(
            email="a@example.com",
            smtp_server="smtp.example.com",
        )
        account_id = str(account["id"])

        self.assertTrue(
            account_mgr.update_account(
                id=account_id,
                updates={"signature": "Best regards"},
            )
        )

        client = _FakeClient()
        await callback_handler(
            client,
            _FakeCallbackUpdate(
                chat_id=123,
                user_id=1,
                message_id=10,
                data=f"account_signature_clear:{account_id}",
            ),
        )

        updated = account_mgr.get_account(id=account_id)
        self.assertIsNone(updated.get("signature"))

    async def test_account_signature_default_ignores_message_not_modified(self):
        from app.email_utils.account_manager import AccountManager
        from app.bot.handlers.callback import callback_handler
        from app.email_utils.signatures import add_account_signature

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
        account = account_mgr.get_account(
            email="a@example.com",
            smtp_server="smtp.example.com",
        )
        account_id = str(account["id"])

        raw = None
        raw, default_id = add_account_signature(raw, name="Default", markdown="Best regards")
        self.assertTrue(account_mgr.update_account(id=account_id, updates={"signature": raw}))

        class _NotModifiedClient(_FakeClient):
            async def edit_text(self, **kwargs):
                self.edits.append(kwargs)
                raise RuntimeError("[Error 400] MESSAGE_NOT_MODIFIED")

        client = _NotModifiedClient()
        await callback_handler(
            client,
            _FakeCallbackUpdate(
                chat_id=123,
                user_id=1,
                message_id=10,
                data=f"account_signature_default:{account_id}:{default_id}",
            ),
        )

        self.assertTrue(client.api.answered)
