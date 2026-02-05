import unittest
from types import SimpleNamespace


class _FakeApi:
    def __init__(self):
        self.captured_html: str | None = None

    async def send_message(self, **kwargs):
        input_message_content = kwargs.get("input_message_content")
        document = getattr(input_message_content, "document", None)
        path = getattr(document, "path", None)
        if path:
            with open(path, "rb") as f:
                self.captured_html = f.read().decode("utf-8")
        return object()


class TestHtmlMetaInjection(unittest.IsolatedAsyncioTestCase):
    async def test_send_html_as_file_does_not_write_literal_backslash_n(self):
        from app.user.email_telegram import EmailTelegramSender

        fake_api = _FakeApi()
        fake_self = SimpleNamespace(bot_client=SimpleNamespace(api=fake_api))

        await EmailTelegramSender.send_html_as_file(
            fake_self,
            chat_id=1,
            thread_id=1,
            content="<html><body><h2>标题</h2></body></html>",
        )

        self.assertIsNotNone(fake_api.captured_html)
        self.assertNotIn("\\n<html", fake_api.captured_html)

