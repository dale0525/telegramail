import datetime
import os
import unittest
from unittest import mock


class _FakeTopicInfo:
    def __init__(self, message_thread_id: int):
        self.message_thread_id = message_thread_id


class _FakeForumTopicDeletedAction:
    ID = "chatEventForumTopicDeleted"

    def __init__(self, thread_id: int):
        self.topic_info = _FakeTopicInfo(message_thread_id=thread_id)


class _FakeChatEvent:
    def __init__(self, event_id: int, date: int, thread_id: int):
        self.id = event_id
        self.date = date
        self.action = _FakeForumTopicDeletedAction(thread_id=thread_id)


class _FakeEventLogResult:
    def __init__(self, events):
        self.events = events


class _FakeTdApi:
    def __init__(self, events):
        self._events = events

    async def get_chat_event_log(self, **_kwargs):
        return _FakeEventLogResult(self._events)


class _FakeUserClient:
    def __init__(self, api):
        self.client = mock.Mock(api=api)


class _FakeDbManager:
    def __init__(self):
        self._cursor_by_chat: dict[int, int] = {}
        self._pending: set[tuple[int, str]] = set()

    def get_chat_event_cursor(self, chat_id: int) -> int:
        return self._cursor_by_chat.get(chat_id, 0)

    def set_chat_event_cursor(self, chat_id: int, event_id: int) -> None:
        self._cursor_by_chat[chat_id] = event_id

    def upsert_deleted_topic(self, chat_id: int, thread_id: str, event_id: int, deleted_at: int) -> bool:
        self._pending.add((chat_id, thread_id))
        return True

    def list_pending_deleted_topics(self, chat_id: int):
        return [{"chat_id": cid, "thread_id": tid} for (cid, tid) in self._pending if cid == chat_id]

    def mark_deleted_topic_processed(self, chat_id: int, thread_id: str) -> None:
        self._pending.discard((chat_id, thread_id))

    def record_deleted_topic_failure(self, chat_id: int, thread_id: str, error: str) -> None:
        # Keep pending; just record would happen in real DB.
        self._pending.add((chat_id, thread_id))

    def get_deletion_targets_for_topic(self, chat_id: int, thread_id: str):
        return {1: {"inbox_uids": ["42"], "outgoing_message_ids": ["<m1@example.com>"]}}


class _FakeAccountManager:
    def get_account(self, id: int):
        return {"id": id, "email": "test@example.com"}


class TestEmailDeleteListener(unittest.IsolatedAsyncioTestCase):
    async def test_passes_request_timeout_to_tdlib(self):
        from app.cron import email_delete_listener as listener

        api = mock.AsyncMock()
        api.get_chat_event_log.return_value = _FakeEventLogResult(events=[])
        fake_user_client = _FakeUserClient(api=api)
        fake_db = _FakeDbManager()

        with (
            mock.patch.dict(
                os.environ, {"TELEGRAM_CHAT_EVENT_LOG_TIMEOUT": "99"}, clear=False
            ),
            mock.patch("app.user.user_client.UserClient", return_value=fake_user_client),
            mock.patch.object(listener, "DBManager", return_value=fake_db),
        ):
            await listener.check_deleted_topics_for_group(chat_id=777)

        self.assertEqual(api.get_chat_event_log.call_args.kwargs["request_timeout"], 99)

    async def test_processes_deleted_topic_even_if_old(self):
        """
        Regression: Previously the listener ignored deletions older than a short time window,
        so topics deleted while the app was down wouldn't delete the server email.
        """
        from app.cron import email_delete_listener as listener

        thread_id = 123
        old_timestamp = int((datetime.datetime.now() - datetime.timedelta(hours=1)).timestamp())

        api = _FakeTdApi(events=[_FakeChatEvent(event_id=9001, date=old_timestamp, thread_id=thread_id)])
        fake_user_client = _FakeUserClient(api=api)
        fake_db = _FakeDbManager()

        imap_instance = mock.Mock()
        imap_instance.delete_email_by_uid.return_value = True
        imap_instance.delete_outgoing_email_by_message_id.return_value = True

        with (
            mock.patch("app.user.user_client.UserClient", return_value=fake_user_client),
            mock.patch.object(listener, "DBManager", return_value=fake_db),
            mock.patch.object(listener, "AccountManager", return_value=_FakeAccountManager()),
            mock.patch.object(listener, "IMAPClient", return_value=imap_instance),
        ):
            await listener.check_deleted_topics_for_group(chat_id=777)

        imap_instance.delete_email_by_uid.assert_called_once_with("42")
        imap_instance.delete_outgoing_email_by_message_id.assert_called_once_with(
            "<m1@example.com>"
        )
