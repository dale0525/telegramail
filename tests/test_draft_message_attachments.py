import os
import tempfile
import unittest


class _FakeSenderId:
    def __init__(self, user_id: int):
        self.user_id = user_id


class _FakeRemote:
    def __init__(self, remote_id: str):
        self.id = remote_id


class _FakeFile:
    def __init__(self, file_id: int, *, size: int = 0, expected_size: int = 0, remote_id: str = ""):
        self.id = file_id
        self.size = size
        self.expected_size = expected_size
        self.remote = _FakeRemote(remote_id) if remote_id else None


class _FakeDocument:
    def __init__(self, *, file_name: str, mime_type: str, file_id: int, size: int, remote_id: str):
        self.file_name = file_name
        self.mime_type = mime_type
        self.document = _FakeFile(file_id, size=size, expected_size=size, remote_id=remote_id)


class _FakeMessageDocument:
    ID = "messageDocument"

    def __init__(self, *, file_name: str, mime_type: str, file_id: int, size: int, remote_id: str):
        self.document = _FakeDocument(
            file_name=file_name,
            mime_type=mime_type,
            file_id=file_id,
            size=size,
            remote_id=remote_id,
        )
        # caption is optional; keep empty
        self.caption = type("_C", (), {"text": ""})()


class _FakeMessage:
    def __init__(self, *, chat_id: int, thread_id: int, user_id: int, content, message_id: int = 0):
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.sender_id = _FakeSenderId(user_id)
        self.content = content
        self.id = int(message_id)


class _FakeUpdate:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeClient:
    def __init__(self):
        self.edits = []
        self.api = type("_Api", (), {})()

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestDraftMessageAttachments(unittest.IsolatedAsyncioTestCase):
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

    async def test_document_message_adds_attachment_to_draft(self):
        from app.database import DBManager

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(draft_id=draft_id, updates={"card_message_id": 99})

        client = _FakeClient()
        attachment_message_id = 1234
        update = _FakeUpdate(
            _FakeMessage(
                chat_id=123,
                thread_id=456,
                user_id=1,
                message_id=attachment_message_id,
                content=_FakeMessageDocument(
                    file_name="a.txt",
                    mime_type="text/plain",
                    file_id=777,
                    size=3,
                    remote_id="remote777",
                ),
            )
        )

        from unittest import mock

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        attachments = db.list_draft_attachments(draft_id=draft_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["file_name"], "a.txt")

        # Regression: attachment messages must be tracked so they can be deleted
        # when the draft is sent (keep the topic tidy).
        tracked_message_ids = db.list_draft_message_ids(draft_id=draft_id)
        self.assertIn(attachment_message_id, tracked_message_ids)

        self.assertTrue(client.edits)
        last_text = client.edits[-1].get("text") or ""
        self.assertTrue(("Attachments:" in last_text) or ("附件:" in last_text))
