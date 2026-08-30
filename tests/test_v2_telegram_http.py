import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path

import httpx

from app.api import create_app
from app.core.config import Settings
from app.integrations.telegram_http import (
    MAX_DOCUMENT_BYTES,
    MiniAppOnlyProjection,
    TelegramApiError,
    TelegramBotApiClient,
    split_html,
    split_text,
    verify_webhook_secret,
)


def telegram_response(result, status_code=200):
    return httpx.Response(status_code, json={"ok": True, "result": result})


class TelegramHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.sleeps = []

    def client(self, handler, **kwargs):
        async def sleep(delay):
            self.sleeps.append(delay)

        async def wrapped(request):
            self.requests.append(request)
            return await handler(request)

        return TelegramBotApiClient(
            "token-that-must-not-appear-in-errors",
            transport=httpx.MockTransport(wrapped),
            sleep=sleep,
            retry_backoff=0.25,
            **kwargs,
        )

    async def test_private_chat_thread_id_is_preserved_for_message(self):
        async def handler(request):
            return telegram_response({"message_id": 7})

        client = self.client(handler)
        try:
            result = await client.send_message(
                1234,
                "hello",
                message_thread_id=88,
                disable_notification=False,
            )
        finally:
            await client.aclose()
        self.assertEqual(result, [{"message_id": 7}])
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["chat_id"], 1234)
        self.assertEqual(body["message_thread_id"], 88)
        self.assertFalse(body["disable_notification"])

    async def test_edit_message_caption_preserves_parse_mode_and_markup(self):
        async def handler(request):
            return telegram_response({"message_id": 7})

        client = self.client(handler)
        try:
            result = await client.edit_message_caption(
                1234, 7, "<b>updated</b>", parse_mode="HTML",
                reply_markup={"inline_keyboard": [[{"text": "Open", "url": "https://example.test"}]]},
            )
        finally:
            await client.aclose()
        self.assertEqual(result, {"message_id": 7})
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["chat_id"], 1234)
        self.assertEqual(body["message_id"], 7)
        self.assertEqual(body["caption"], "<b>updated</b>")
        self.assertEqual(body["parse_mode"], "HTML")
        self.assertIn("inline_keyboard", body["reply_markup"])

    async def test_send_document_serializes_nested_multipart_fields(self):
        markup = {"inline_keyboard": [[{"text": "Open", "url": "https://example.test"}]]}

        async def handler(request):
            body = request.content.decode()
            self.assertIn('name="reply_markup"', body)
            self.assertIn(
                '{"inline_keyboard":[[{"text":"Open","url":"https://example.test"}]]}',
                body,
            )
            return telegram_response({"message_id": 7})

        client = self.client(handler)
        try:
            result = await client.send_document(
                1234,
                b"<p>mail</p>",
                filename="mail.html",
                content_type="text/html",
                message_thread_id=88,
                caption="<b>mail</b>",
                parse_mode="HTML",
                disable_notification=False,
                reply_markup=markup,
            )
        finally:
            await client.aclose()
        self.assertEqual(result, {"message_id": 7})

    def test_settings_read_dedicated_bot_api_proxy(self):
        with patch.dict(
            os.environ,
            {
                "SESSION_SECRET": "s" * 32,
                "TELEGRAMAIL_BOT_API_PROXY": "http://proxy.example.test:7890",
            },
            clear=True,
        ):
            self.assertEqual(
                Settings.from_env().telegram_bot_api_proxy,
                "http://proxy.example.test:7890",
            )

    async def test_rate_limit_retries_after_server_value(self):
        responses = [
            httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
            telegram_response(True),
        ]

        async def handler(_request):
            return responses.pop(0)

        client = self.client(handler)
        try:
            self.assertTrue(await client.delete_webhook())
        finally:
            await client.aclose()
        self.assertEqual(self.sleeps, [3.0])

    async def test_5xx_uses_exponential_backoff(self):
        responses = [httpx.Response(502, text="bad gateway"), telegram_response(True)]

        async def handler(_request):
            return responses.pop(0)

        client = self.client(handler)
        try:
            self.assertTrue(await client.answer_callback_query("callback"))
        finally:
            await client.aclose()
        self.assertEqual(self.sleeps, [0.25])

    async def test_timeout_is_ambiguous_and_never_retried(self):
        async def handler(_request):
            raise httpx.ReadTimeout("late response")

        client = self.client(handler)
        try:
            with self.assertRaises(TelegramApiError) as raised:
                await client.delete_webhook()
        finally:
            await client.aclose()
        self.assertTrue(raised.exception.ambiguous)
        self.assertEqual(self.sleeps, [])

    async def test_topic_delete_is_gated_and_sends_private_thread_id(self):
        async def handler(request):
            if request.url.path.endswith("getMe"):
                return telegram_response({"id": 1, "has_topics_enabled": True})
            return telegram_response(True)

        client = self.client(handler)
        try:
            self.assertTrue(await client.delete_forum_topic(1234, 88))
        finally:
            await client.aclose()
        self.assertTrue(self.requests[-1].url.path.endswith("deleteForumTopic"))
        self.assertEqual(json.loads(self.requests[-1].content), {"chat_id": 1234, "message_thread_id": 88})

    async def test_document_larger_than_50_mb_returns_mini_app_projection(self):
        async def handler(_request):
            self.fail("oversized document must not be uploaded")

        client = self.client(handler)
        try:
            result = await client.send_document(1, b"x" * (MAX_DOCUMENT_BYTES + 1), filename="large.bin")
        finally:
            await client.aclose()
        self.assertIsInstance(result, MiniAppOnlyProjection)
        self.assertTrue(result.mini_app_only)
        self.assertEqual(result.filename, "large.bin")

    async def test_dispatcher_handles_only_start_and_callback(self):
        methods = []

        async def handler(request):
            methods.append(request.url.path.rsplit("/", 1)[-1])
            return telegram_response(True)

        client = self.client(handler, mini_app_url="https://mail.example.test")
        try:
            await client.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/start"}})
            await client.handle_update({"update_id": 2, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/ignored-one"}})
            await client.handle_update({"update_id": 3, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/ignored-two"}})
            await client.handle_update({"update_id": 4, "callback_query": {"id": "callback-1", "from": {"id": 42}, "data": "ignored"}})
        finally:
            await client.aclose()

        self.assertEqual(
            methods,
            ["setChatMenuButton", "sendMessage", "answerCallbackQuery"],
        )

    async def test_set_my_commands_replaces_legacy_menu_with_start_only(self):
        async def handler(_request):
            return telegram_response(True)

        client = self.client(handler)
        try:
            self.assertTrue(await client.set_my_commands([
                {"command": "start", "description": "打开 Telegramail"},
            ]))
        finally:
            await client.aclose()

        self.assertTrue(self.requests[-1].url.path.endswith("setMyCommands"))
        self.assertEqual(json.loads(self.requests[-1].content), {
            "commands": [{"command": "start", "description": "打开 Telegramail"}],
        })


class _FlakyDispatcher:
    def __init__(self):
        self.calls = 0

    async def handle_update(self, _update):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient handler failure")


class _WebhookDatabase:
    """Focused SQLite fake: reproduces the webhook table's production contract."""

    def __init__(self, path):
        self.path = str(path)
        self.db = self
        with self.connect() as connection:
            connection.execute(
                """CREATE TABLE bot_updates (
                    update_id INTEGER PRIMARY KEY,
                    update_type TEXT,
                    payload_json TEXT,
                    status TEXT NOT NULL DEFAULT 'received',
                    received_at INTEGER NOT NULL,
                    processed_at INTEGER,
                    error_message TEXT
                )"""
            )

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def transaction(self, *, immediate=False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class TelegramWebhookDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewer_reproduction_reclaims_failed_and_expired_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = _WebhookDatabase(Path(directory) / "webhook.db")
            dispatcher = _FlakyDispatcher()
            app = create_app(
                Settings(session_secret="s" * 32, webhook_secret="webhook-secret", secure_cookies=False, web_dist=None),
                db=repository,
                telegram=dispatcher,
            )
            headers = {"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                update = {"update_id": 700, "message": {"text": "/start"}}
                self.assertEqual((await http.post("/api/v1/telegram/webhook", headers=headers, json=update)).status_code, 503)
                self.assertEqual((await http.post("/api/v1/telegram/webhook", headers=headers, json=update)).status_code, 204)
                self.assertEqual((await http.post("/api/v1/telegram/webhook", headers=headers, json=update)).status_code, 204)

                with repository.db.transaction(immediate=True) as connection:
                    connection.execute(
                        "INSERT INTO bot_updates(update_id, update_type, payload_json, status, received_at) VALUES (?, ?, ?, 'processing', ?)",
                        (701, "message", "{}", int(time.time()) - 61),
                    )
                self.assertEqual(
                    (await http.post("/api/v1/telegram/webhook", headers=headers, json={"update_id": 701, "message": {"text": "/ignored"}})).status_code,
                    204,
                )

            connection = repository.db.connect()
            try:
                failed_then_processed = connection.execute("SELECT status FROM bot_updates WHERE update_id = 700").fetchone()
                reclaimed = connection.execute("SELECT status FROM bot_updates WHERE update_id = 701").fetchone()
            finally:
                connection.close()
            self.assertEqual(dispatcher.calls, 3)
            self.assertEqual(failed_then_processed["status"], "processed")
            self.assertEqual(reclaimed["status"], "processed")


class TelegramTextTests(unittest.TestCase):
    def test_plain_text_chunks_do_not_exceed_telegram_limit(self):
        chunks = split_text("x" * 8193)
        self.assertEqual([len(chunk) for chunk in chunks], [4096, 4096, 1])
        self.assertEqual("".join(chunks), "x" * 8193)

    def test_html_chunks_stay_balanced_and_within_rendered_limit(self):
        chunks = split_html("<b>" + ("x" * 4097) + "</b>")
        self.assertEqual(chunks, ["<b>" + ("x" * 4096) + "</b>", "<b>x</b>"])

    def test_webhook_secret_comparison_is_case_insensitive_for_header_name(self):
        self.assertTrue(verify_webhook_secret({"X-Telegram-Bot-Api-Secret-Token": "secret"}, "secret"))
        self.assertFalse(verify_webhook_secret({"X-Telegram-Bot-Api-Secret-Token": "wrong"}, "secret"))


if __name__ == "__main__":
    unittest.main()
