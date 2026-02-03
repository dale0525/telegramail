import unittest


class _FakeDBManager:
    def __init__(self, events: list[str], update_ok: bool = True):
        self._events = events
        self._update_ok = update_ok

    def update_thread_id_in_db(self, email_id: int, thread_id: int) -> bool:
        self._events.append(f"db_update:{email_id}:{thread_id}")
        return self._update_ok


class _FakeEmailSender:
    def __init__(self, events: list[str]):
        self._events = events
        self.db_manager = _FakeDBManager(events)
        self._sent_formatted_texts = []

    async def get_thread_id_by_subject(self, clean_subject: str, account_id: int):
        self._events.append("get_thread")
        return None

    async def create_forum_topic(self, chat_id: int, title: str):
        self._events.append(f"create_topic:{chat_id}:{title}")
        return 111

    async def str_to_formatted(self, original: str, parse_mode):
        self._events.append(f"parse:{parse_mode}:{original}")
        # Default behavior is "successfully parsed"
        from aiotdlib.api import FormattedText

        return FormattedText(text=original, entities=[])

    async def send_text_message(self, **kwargs):
        # Legacy path (current production behavior)
        self._events.append("send_text_message")
        return object()

    async def send_formatted_text_message(
        self, *, chat_id: int, formatted_text, send_notification: bool, thread_id: int, urls
    ):
        self._events.append("send_formatted_text_message")
        self._sent_formatted_texts.append(formatted_text)
        return object()

    async def send_html_as_file(self, **kwargs):
        self._events.append("send_html_as_file")
        return object()

    async def send_attachment(self, **kwargs):
        self._events.append("send_attachment")
        return object()


class TestAtomicEmailSenderOrdering(unittest.IsolatedAsyncioTestCase):
    async def test_parses_all_messages_before_creating_topic(self):
        from app.user.email_telegram import AtomicEmailSender, MessageContent

        events: list[str] = []
        fake_sender = _FakeEmailSender(events)
        atomic_sender = AtomicEmailSender(fake_sender)

        messages = [
            MessageContent(text="*Hello*", parse_mode="Markdown", send_notification=False)
        ]

        ok = await atomic_sender.send_email_atomically(
            chat_id=123,
            topic_title="Test topic",
            messages=messages,
            files=[],
            attachments=[],
            email_id=1,
            account_id=1,
        )

        self.assertTrue(ok)
        self.assertTrue(any(e.startswith("parse:") for e in events))
        self.assertTrue(any(e.startswith("create_topic:") for e in events))

        first_parse_index = next(i for i, e in enumerate(events) if e.startswith("parse:"))
        first_create_index = next(
            i for i, e in enumerate(events) if e.startswith("create_topic:")
        )
        self.assertLess(first_parse_index, first_create_index)

    async def test_parse_failure_falls_back_and_still_sends(self):
        from app.user.email_telegram import AtomicEmailSender, MessageContent

        events: list[str] = []
        fake_sender = _FakeEmailSender(events)

        async def _raise_on_parse(original: str, parse_mode):
            events.append(f"parse_raises:{parse_mode}:{original}")
            raise ValueError("bad markup")

        fake_sender.str_to_formatted = _raise_on_parse  # type: ignore[assignment]

        # Simulate the current legacy path failing (e.g. Telegram parse_text_entities error)
        async def _always_fail_send_text_message(**kwargs):
            events.append("send_text_message_failed")
            return None

        fake_sender.send_text_message = _always_fail_send_text_message  # type: ignore[assignment]

        atomic_sender = AtomicEmailSender(fake_sender)
        messages = [
            MessageContent(
                text="<b>bad</b>",
                parse_mode="HTML",
                send_notification=True,
            )
        ]

        ok = await atomic_sender.send_email_atomically(
            chat_id=123,
            topic_title="Test topic",
            messages=messages,
            files=[],
            attachments=[],
            email_id=1,
            account_id=1,
        )

        self.assertTrue(ok)
        self.assertIn("send_formatted_text_message", events)
        self.assertEqual(len(fake_sender._sent_formatted_texts), 1)
        self.assertEqual(fake_sender._sent_formatted_texts[0].text, "bad")

