import os
import tempfile
import unittest
from unittest import mock


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

    async def send_text(self, *args, **kwargs):
        raise AssertionError("send_text should not be used by mailbox picker UI")


class TestAccountMailboxPicker(unittest.IsolatedAsyncioTestCase):
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

    async def test_picker_set_renders_buttons_without_manual_input(self):
        from app.email_utils.account_manager import AccountManager
        from app.bot.handlers.callback import callback_handler
        from app.email_utils.imap_client import IMAPClient

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
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"account_mailboxes_set:{account_id}"
        )

        with mock.patch.object(
            IMAPClient,
            "list_mailboxes",
            return_value=[
                {"name": "INBOX", "attrs": "", "selectable": True},
                {"name": "Archive", "attrs": "", "selectable": True},
            ],
        ):
            await callback_handler(client, update)

        self.assertTrue(client.edits)
        self.assertIn("reply_markup", client.edits[-1])

    async def test_picker_toggle_and_save_updates_account_override(self):
        from app.email_utils.account_manager import AccountManager
        from app.bot.handlers.callback import callback_handler
        from app.email_utils.imap_client import IMAPClient

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

        with mock.patch.object(
            IMAPClient,
            "list_mailboxes",
            return_value=[
                {"name": "INBOX", "attrs": "", "selectable": True},
                {"name": "Archive", "attrs": "", "selectable": True},
            ],
        ):
            # Start picker
            await callback_handler(
                client,
                _FakeCallbackUpdate(
                    chat_id=123,
                    user_id=1,
                    message_id=10,
                    data=f"account_mailboxes_set:{account_id}",
                ),
            )
            # Toggle Archive (index 1)
            await callback_handler(
                client,
                _FakeCallbackUpdate(
                    chat_id=123,
                    user_id=1,
                    message_id=10,
                    data=f"account_mailboxes_toggle:{account_id}:1",
                ),
            )
            # Save
            await callback_handler(
                client,
                _FakeCallbackUpdate(
                    chat_id=123,
                    user_id=1,
                    message_id=10,
                    data=f"account_mailboxes_save:{account_id}",
                ),
            )

        updated = account_mgr.get_account(id=account_id)
        self.assertEqual(updated.get("imap_monitored_mailboxes"), "INBOX,Archive")

