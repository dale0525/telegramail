import unittest
from unittest import mock

from aiotdlib.api.errors import error as td_error


class _FakeApi:
    def __init__(self):
        self.checked_chat_ids: list[int] = []

    async def get_chat(self, chat_id: int):
        self.checked_chat_ids.append(chat_id)
        raise td_error.BadRequest(400, "Chat not found")


class _FakeAccountManager:
    def __init__(self):
        self.account = {
            "id": 1,
            "email": "test@example.com",
            "alias": "Test Mail",
            "tg_group_id": 123,
        }
        self.updated: list[tuple[int, dict]] = []

    def get_account(self, id: int):
        if id == self.account["id"]:
            return dict(self.account)
        return None

    def update_account(self, updates: dict, id: int):
        self.updated.append((id, dict(updates)))
        self.account.update(updates)
        return True


class _FakeDbManager:
    def find_thread_id_for_reply_headers(self, **_kwargs):
        return None


class _FakeAtomicEmailSender:
    instances = []

    def __init__(self, _sender):
        self.thread_id = None
        self.calls = []
        self.__class__.instances.append(self)

    async def send_email_atomically(self, **kwargs):
        self.calls.append(kwargs)
        return True


class _FakeGroup:
    id = 999


class TestEmailTelegramDeliveryGroup(unittest.IsolatedAsyncioTestCase):
    async def test_recreates_account_group_when_configured_chat_is_missing(self):
        from app.user import email_telegram
        from app.user.email_telegram import EmailTelegramSender

        account_manager = _FakeAccountManager()
        api = _FakeApi()
        sender = EmailTelegramSender.__new__(EmailTelegramSender)
        sender.bot_client = mock.Mock(api=api)
        sender.db_manager = _FakeDbManager()
        sender.prepare_email_messages = mock.Mock(return_value=[])
        sender.prepare_email_files = mock.Mock(return_value=[])
        sender.prepare_email_attachments = mock.Mock(return_value=[])
        _FakeAtomicEmailSender.instances.clear()

        async def _create_group(**kwargs):
            self.assertEqual(kwargs["name"], "Test Mail")
            self.assertEqual(kwargs["desc"], "test@example.com")
            return _FakeGroup()

        with (
            mock.patch.object(
                email_telegram, "AccountManager", return_value=account_manager
            ),
            mock.patch.object(
                email_telegram, "AtomicEmailSender", _FakeAtomicEmailSender
            ),
            mock.patch("app.bot.utils._create_super_group", side_effect=_create_group),
        ):
            ok = await sender.send_email_to_telegram(
                {
                    "id": 42,
                    "email_account": 1,
                    "subject": "Hello",
                    "in_reply_to": None,
                    "references_header": None,
                }
            )

        self.assertTrue(ok)
        self.assertEqual(api.checked_chat_ids, [123])
        self.assertEqual(account_manager.updated, [(1, {"tg_group_id": 999})])
        self.assertEqual(
            _FakeAtomicEmailSender.instances[0].calls[0]["chat_id"],
            999,
        )
