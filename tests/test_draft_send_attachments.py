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
    def __init__(self, file_path: str):
        self.file_path = file_path

    async def answer_callback_query(self, callback_query_id: int, text: str, url: str, cache_time: int):
        return None

    async def download_file(self, file_id: int, priority: int, offset: int, limit: int, synchronous: bool = False):
        local = type(
            "_Local",
            (),
            {"path": self.file_path, "is_downloading_completed": True},
        )()
        return type("_File", (), {"id": file_id, "local": local, "size": 3, "expected_size": 3})()


class _FakeClient:
    def __init__(self, api: _FakeApi):
        self.api = api
        self.edits = []

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestDraftSendAttachments(unittest.IsolatedAsyncioTestCase):
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

    async def test_send_downloads_and_passes_attachments_to_smtp(self):
        from app.database import DBManager
        from app.bot.handlers.callback import callback_handler

        # Create a temp file to simulate downloaded telegram file.
        file_path = os.path.join(self._tmp.name, "a.txt")
        with open(file_path, "wb") as f:
            f.write(b"abc")

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
        db.add_draft_attachment(
            draft_id=draft_id,
            file_id=777,
            remote_id="remote777",
            file_type="document",
            file_name="a.txt",
            mime_type="text/plain",
            size=3,
        )

        called = {}

        class _FakeSMTPClient:
            def __init__(self, **kwargs):
                called["init"] = kwargs

            def send_email_sync(self, **kwargs):
                called["send"] = kwargs
                return True

        client = _FakeClient(api=_FakeApi(file_path=file_path))
        update = _FakeCallbackUpdate(
            chat_id=123, user_id=1, message_id=10, data=f"draft:send:{draft_id}"
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.callbacks.drafts.SMTPClient", _FakeSMTPClient):
            await callback_handler(client, update)

        self.assertIn("send", called)
        attachments = called["send"].get("attachments") or []
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "a.txt")
        self.assertEqual(attachments[0]["mime_type"], "text/plain")
        self.assertEqual(attachments[0]["data"], b"abc")
