from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.db import V2Repository
from app.integrations.mail.telegram import MailTelegramProjection, TelegramTopic
from app.integrations.mail.types import IncomingMail
from app.integrations.telegram_http import TelegramApiError, split_html
from app.services.telegram_mail_ui import TelegramMailBotUi


class _Telegram:
    def __init__(self):
        self.sent = []
        self.edited_text = []
        self.edited_markup = []
        self.pinned = []
        self.deleted = []
        self.deleted_topics = []

    async def send_message(self, chat_id, text, **extra):
        self.sent.append((chat_id, text, extra))
        return [{"message_id": 700 + len(self.sent)}]

    async def edit_message_text(self, chat_id, message_id, text, **extra):
        self.edited_text.append((chat_id, message_id, text, extra))
        return True

    async def edit_message_reply_markup(self, chat_id, message_id, **extra):
        self.edited_markup.append((chat_id, message_id, extra))
        return True

    async def pin_chat_message(self, chat_id, message_id, **extra):
        self.pinned.append((chat_id, message_id, extra))
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.deleted_topics.append((chat_id, message_thread_id))
        return True


class TelegramMailUiTests(unittest.IsolatedAsyncioTestCase):
    def make_repository(self, directory: str):
        repo = V2Repository(Path(directory) / "ui.db", master_key=b"u" * 32)
        account = repo.create_account(
            {
                "email": "owner@example.test",
                "imap_server": "imap.example.test",
                "imap_port": 993,
                "smtp_server": "smtp.example.test",
                "smtp_port": 465,
            },
            "password",
        )
        repo.bind_admin(42, private_chat_id=42)
        with repo.db.transaction(immediate=True) as conn:
            thread_id = int(conn.execute(
                """INSERT INTO mail_threads(account_id, subject_normalized, telegram_chat_id,
                   telegram_message_thread_id, created_at, updated_at)
                   VALUES (?, 'hello', 42, 99, 1, 1)""",
                (int(account["id"]),),
            ).lastrowid)
            conn.execute(
                """INSERT INTO emails(account_id, thread_id, mailbox, uidvalidity, uid, sender,
                   subject, body_text, created_at, updated_at)
                   VALUES (?, ?, 'INBOX', 'epoch-1', '101', 'sender@example.test',
                           'Hello', 'A readable body', 1, 1)""",
                (int(account["id"]), thread_id),
            )
        return repo, thread_id

    async def test_commands_do_not_create_an_all_messages_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self.make_repository(directory)
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo, mini_app_url="https://mail.example.test")
            ui.bind_client(telegram)
            start = {"chat": {"id": 42}, "from": {"id": 42}, "text": "/start"}
            inbox = {"chat": {"id": 42}, "from": {"id": 42}, "text": "/inbox"}

            self.assertFalse(await ui.handle_message(start))
            self.assertFalse(await ui.handle_message(inbox))

            self.assertEqual(telegram.sent, [])

    async def test_startup_retires_the_stored_all_messages_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self.make_repository(directory)
            repo.set_inbox_panel_message(42, 42, 701, cursor="old", search="invoice")
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)

            self.assertTrue(await ui.retire_inbox_panel())

            self.assertEqual(telegram.deleted, [(42, 701)])
            binding = repo.get_admin_binding()
            self.assertIsNone(binding["inbox_panel_chat_id"])
            self.assertIsNone(binding["inbox_panel_message_id"])
            self.assertIsNone(binding["inbox_panel_cursor"])
            self.assertIsNone(binding["inbox_panel_search"])

    async def test_delivery_posts_a_fresh_panel_at_the_latest_chat_position(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self.make_repository(directory)
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)

            await ui.show_latest_inbox()
            await ui.show_latest_inbox()

            self.assertEqual(len(telegram.sent), 2)
            self.assertEqual(telegram.sent[0][0], 42)
            self.assertEqual(telegram.sent[1][0], 42)
            self.assertEqual(len(telegram.edited_text), 0)
            self.assertEqual(telegram.deleted, [(42, 701)])

    async def test_panel_delete_is_confirmed_and_queues_one_durable_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            base = {
                "id": "callback",
                "from": {"id": 42},
                "message": {"chat": {"id": 42}, "message_id": 7},
            }

            ask = {**base, "data": f"tm:delete:ask:{thread_id}:panel:0"}
            self.assertIn("再次确认", (await ui.handle_callback(ask))["text"])
            self.assertEqual(len(telegram.edited_markup), 0)
            self.assertIn("确认删除这封邮件", telegram.edited_text[-1][2])
            self.assertIn("<b>主题：</b>Hello", telegram.edited_text[-1][2])
            self.assertIn("<b>发件人：</b>sender@example.test", telegram.edited_text[-1][2])
            confirm_button = telegram.edited_text[-1][3]["reply_markup"]["inline_keyboard"][0][0]
            self.assertEqual(
                confirm_button["callback_data"],
                f"tm:delete:confirm:{thread_id}:panel:0",
            )

            confirm = {**base, "data": f"tm:delete:confirm:{thread_id}:panel:0"}
            self.assertEqual((await ui.handle_callback(confirm))["text"], "正在删除邮件")
            self.assertEqual((await ui.handle_callback(confirm))["text"], "正在删除邮件")

            conn = repo.db.connect()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM delete_operations").fetchone()[0], 1)
                self.assertEqual(
                    1,
                    conn.execute(
                        "SELECT topic_delete_requested FROM delete_operations"
                    ).fetchone()["topic_delete_requested"],
                )
            finally:
                conn.close()
            self.assertIn("暂无邮件", telegram.edited_text[-1][2])
            self.assertEqual(telegram.deleted_topics, [(42, 99)])

    async def test_topic_delete_returns_before_slow_topic_removal_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            repo.set_inbox_panel_message(42, 42, 8)

            class SlowTelegram(_Telegram):
                def __init__(self):
                    super().__init__()
                    self.started = asyncio.Event()
                    self.release = asyncio.Event()

                async def delete_forum_topic(self, chat_id, message_thread_id):
                    self.started.set()
                    await self.release.wait()
                    return await super().delete_forum_topic(chat_id, message_thread_id)

            telegram = SlowTelegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            callback = {
                "id": "callback",
                "from": {"id": 42},
                "data": f"tm:delete:confirm:{thread_id}:topic:0",
                "message": {
                    "chat": {"id": 42},
                    "message_id": 7,
                    "message_thread_id": 99,
                },
            }

            result = await asyncio.wait_for(ui.handle_callback(callback), timeout=0.5)

            self.assertEqual(result["text"], "正在删除邮件")
            self.assertTrue(telegram.started.is_set())
            self.assertEqual(telegram.deleted_topics, [])
            self.assertIn("暂无邮件", telegram.edited_text[-1][2])
            telegram.release.set()
            await asyncio.gather(*tuple(ui._delete_tasks))
            self.assertEqual(telegram.deleted_topics, [(42, 99)])
            operation = repo.get_delete(1)
            self.assertEqual(1, operation["topic_deleted"])

    async def test_panel_delete_returns_before_slow_panel_refresh_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)

            class SlowTelegram(_Telegram):
                def __init__(self):
                    super().__init__()
                    self.started = asyncio.Event()
                    self.release = asyncio.Event()

                async def edit_message_text(self, chat_id, message_id, text, **extra):
                    if message_id == 7 and "暂无邮件" in text:
                        self.started.set()
                        await self.release.wait()
                    return await super().edit_message_text(chat_id, message_id, text, **extra)

            telegram = SlowTelegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            base = {
                "id": "callback",
                "from": {"id": 42},
                "message": {"chat": {"id": 42}, "message_id": 7},
            }
            await ui.handle_callback({**base, "data": f"tm:delete:ask:{thread_id}:panel:0"})
            result = await asyncio.wait_for(
                ui.handle_callback({**base, "data": f"tm:delete:confirm:{thread_id}:panel:0"}),
                timeout=0.5,
            )

            self.assertEqual(result["text"], "正在删除邮件")
            self.assertTrue(telegram.started.is_set())
            self.assertNotIn("暂无邮件", telegram.edited_text[-1][2])
            telegram.release.set()
            await asyncio.gather(*tuple(ui._delete_tasks))
            self.assertIn("暂无邮件", telegram.edited_text[-1][2])

    async def test_panel_delete_retries_a_transient_hidden_refresh_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)

            class FlakyTelegram(_Telegram):
                def __init__(self):
                    super().__init__()
                    self.hidden_attempts = 0

                async def edit_message_text(self, chat_id, message_id, text, **extra):
                    if "暂无邮件" in text:
                        self.hidden_attempts += 1
                        if self.hidden_attempts == 1:
                            raise TelegramApiError(
                                "editMessageText", "temporary upstream failure"
                            )
                    return await super().edit_message_text(
                        chat_id, message_id, text, **extra
                    )

            telegram = FlakyTelegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            base = {
                "id": "callback",
                "from": {"id": 42},
                "message": {"chat": {"id": 42}, "message_id": 7},
            }
            await ui.handle_callback(
                {**base, "data": f"tm:delete:ask:{thread_id}:panel:0"}
            )
            result = await asyncio.wait_for(
                ui.handle_callback(
                    {**base, "data": f"tm:delete:confirm:{thread_id}:panel:0"}
                ),
                timeout=0.5,
            )

            self.assertEqual("正在删除邮件", result["text"])
            await asyncio.gather(*tuple(ui._delete_tasks))
            self.assertEqual(2, telegram.hidden_attempts)
            self.assertIn("暂无邮件", telegram.edited_text[-1][2])

    async def test_confirm_delete_wakes_the_delete_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            wakes = []
            ui.bind_worker_wake(lambda: wakes.append(True))
            base = {
                "id": "callback",
                "from": {"id": 42},
                "message": {"chat": {"id": 42}, "message_id": 7},
            }

            await ui.handle_callback({**base, "data": f"tm:delete:confirm:{thread_id}:panel:0"})

            self.assertEqual(wakes, [True])

    async def test_cancel_panel_delete_restores_the_selected_mail_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            base = {
                "id": "callback",
                "from": {"id": 42},
                "message": {"chat": {"id": 42}, "message_id": 7},
            }

            await ui.handle_callback(
                {**base, "data": f"tm:delete:ask:{thread_id}:panel:0"}
            )
            result = await ui.handle_callback(
                {**base, "data": f"tm:delete:cancel:{thread_id}:panel:0"}
            )

            self.assertEqual(result["text"], "已取消")
            self.assertIn("<b>Hello</b>", telegram.edited_text[-1][2])
            self.assertIn("返回全部", str(telegram.edited_text[-1][3]["reply_markup"]))
            conn = repo.db.connect()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM delete_operations").fetchone()[0], 0)
            finally:
                conn.close()

    async def test_cancel_topic_delete_restores_important_link_buttons(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            with repo.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE emails SET llm_important_links_json = ? WHERE thread_id = ?",
                    ('[{"caption":"打开账单","link":"https://example.test/bill"}]', thread_id),
                )
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            base = {
                "id": "callback",
                "from": {"id": 42},
                "message": {"chat": {"id": 42}, "message_id": 7, "message_thread_id": 99},
            }

            await ui.handle_callback(
                {**base, "data": f"tm:delete:ask:{thread_id}:topic:0"}
            )
            result = await ui.handle_callback(
                {**base, "data": f"tm:delete:cancel:{thread_id}:topic:0"}
            )

            self.assertEqual(result["text"], "已取消")
            markup = telegram.edited_markup[-1][2]["reply_markup"]["inline_keyboard"]
            self.assertEqual(markup[0][0]["text"], "🗑 删除邮件")
            self.assertEqual(markup[1][0], {
                "text": "打开账单",
                "url": "https://example.test/bill",
            })

    async def test_detail_does_not_claim_an_undelivered_topic_contains_html_or_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            ui = TelegramMailBotUi(repo)

            detail, _ = ui._render_detail(thread_id, 0)

            self.assertIn("尚未投递到 Telegram Topic", detail)
            self.assertNotIn("原始 HTML 和附件保存在", detail)

    async def test_detail_renders_only_safe_summary_markup(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            with repo.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE emails SET llm_summary = ? WHERE thread_id = ?",
                    ("<b>重点</b><i onclick='bad'>说明</i><a href='https://evil.invalid'>链接</a>", thread_id),
                )
            detail, _ = TelegramMailBotUi(repo)._render_detail(thread_id, 0)

            self.assertIn("<b>重点</b><i>说明</i>链接", detail)
            self.assertNotIn("onclick", detail)
            self.assertNotIn("evil.invalid", detail)

    async def test_detail_reports_a_confirmed_topic_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            with repo.db.transaction(immediate=True) as conn:
                email_id = int(conn.execute(
                    "SELECT id FROM emails WHERE thread_id = ?", (thread_id,)
                ).fetchone()["id"])
                conn.execute(
                    """INSERT INTO telegram_delivery_parts(
                           email_id, part_index, telegram_chat_id, telegram_message_id,
                           status, created_at, updated_at
                       ) VALUES (?, 0, 42, 321, 'delivered', 1, 1)""",
                    (email_id,),
                )
            ui = TelegramMailBotUi(repo)

            detail, _ = ui._render_detail(thread_id, 0)

            self.assertIn("已投递到 Telegram Topic", detail)

    async def test_view_renders_enter_and_explicit_rebuild_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)

            class Telegram(_Telegram):
                async def get_me(self):
                    return {"username": "LogicEmailBot"}

            telegram = Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)

            result = await ui.handle_callback({
                "id": "callback",
                "from": {"id": 42},
                "data": f"tm:inbox:view:{thread_id}:0",
                "message": {"chat": {"id": 42}, "message_id": 7},
            })

            self.assertNotIn("url", result)
            self.assertEqual(result, {})
            topic_button = telegram.edited_text[-1][3]["reply_markup"]["inline_keyboard"][1][0]
            self.assertEqual(topic_button, {
                "text": "进入 Topic",
                "url": "https://t.me/LogicEmailBot/99",
            })
            self.assertEqual(telegram.deleted, [])
            rebuild_button = telegram.edited_text[-1][3]["reply_markup"]["inline_keyboard"][2][0]
            self.assertEqual(rebuild_button, {
                "text": "重建 Topic",
                "callback_data": f"tm:inbox:rebuild:{thread_id}:0:99",
            })

    async def test_view_topic_links_to_the_verified_topic_not_a_stale_message(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            with repo.db.transaction(immediate=True) as conn:
                email_id = int(conn.execute(
                    "SELECT id FROM emails WHERE thread_id = ?", (thread_id,)
                ).fetchone()["id"])
                conn.execute(
                    """INSERT INTO telegram_delivery_parts(
                           email_id, part_index, telegram_chat_id, telegram_message_id,
                           status, created_at, updated_at
                       ) VALUES (?, 0, 42, 321, 'delivered', 1, 1)""",
                    (email_id,),
                )
            class Telegram(_Telegram):
                async def get_me(self):
                    return {"username": "LogicEmailBot"}

            telegram = Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)

            result = await ui.handle_callback({
                "id": "callback",
                "from": {"id": 42},
                "data": f"tm:inbox:view:{thread_id}:0",
                "message": {"chat": {"id": 42}, "message_id": 7},
            })

            self.assertNotIn("url", result)
            topic_button = telegram.edited_text[-1][3]["reply_markup"]["inline_keyboard"][1][0]
            self.assertEqual(topic_button["url"], "https://t.me/LogicEmailBot/99")

    async def test_rebuild_replaces_a_ghost_topic_after_history_was_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            created = []
            with repo.db.transaction(immediate=True) as conn:
                email_id = int(conn.execute(
                    "SELECT id FROM emails WHERE thread_id = ?", (thread_id,)
                ).fetchone()["id"])
                conn.execute(
                    """INSERT INTO telegram_delivery_parts(
                           email_id, part_index, telegram_chat_id, telegram_message_id,
                           status, created_at, updated_at
                       ) VALUES (?, 0, 42, 321, 'delivered', 1, 1)""",
                    (email_id,),
                )

            class Telegram(_Telegram):
                async def send_message(self, chat_id, text, **extra):
                    if extra.get("message_thread_id") is not None:
                        return [{
                            "message_id": 800,
                            "message_thread_id": extra["message_thread_id"],
                        }]
                    return await super().send_message(chat_id, text, **extra)

                async def create_forum_topic(self, chat_id, name):
                    created.append((chat_id, name))
                    return {"message_thread_id": 777}

                async def get_me(self):
                    return {"username": "LogicEmailBot"}

            ui = TelegramMailBotUi(repo)
            ui.bind_client(Telegram())
            wakes = []
            ui.bind_worker_wake(lambda: wakes.append(True))

            callback = {
                "id": "callback",
                "from": {"id": 42},
                "data": f"tm:inbox:rebuild:{thread_id}:0:99",
                "message": {"chat": {"id": 42}, "message_id": 7},
            }
            result = await ui.handle_callback(callback)
            duplicate = await ui.handle_callback(callback)

            self.assertNotIn("url", result)
            self.assertIn("Topic 已重建", result["text"])
            self.assertIn("已经重建", duplicate["text"])
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0], (42, "sender@example.test · Hello"))
            self.assertEqual(wakes, [True])
            topic_button = ui.client.edited_text[-1][3]["reply_markup"]["inline_keyboard"][1][0]
            self.assertEqual(topic_button, {
                "text": "进入 Topic",
                "url": "https://t.me/LogicEmailBot/777",
            })
            thread = repo.get_telegram_inbox_thread(thread_id)
            self.assertEqual(thread["telegram_message_thread_id"], 777)
            self.assertEqual(thread["telegram_delivery_status"], "queued")

    async def test_topic_delete_rejects_a_mismatched_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)
            result = await ui.handle_callback({
                "id": "callback",
                "from": {"id": 42},
                "data": f"tm:delete:ask:{thread_id}:topic:0",
                "message": {"chat": {"id": 42}, "message_id": 7, "message_thread_id": 100},
            })
            self.assertTrue(result["show_alert"])
            self.assertEqual(telegram.edited_markup, [])

    async def test_panel_and_detail_never_split_for_pathological_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            with repo.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE emails SET subject = ?, sender = ?, body_text = ? WHERE thread_id = ?",
                    ("<&>" * 4000, "sender&" * 2000, "body" * 3000, thread_id),
                )
            ui = TelegramMailBotUi(repo)

            panel, _ = ui._render_inbox(0)
            detail, _ = ui._render_detail(thread_id, 0)

            self.assertEqual(len(split_html(panel)), 1)
            self.assertEqual(len(split_html(detail)), 1)

    async def test_mail_projection_keeps_one_card_with_delete_controls(self):
        telegram = _Telegram()
        projection = MailTelegramProjection(telegram, 42)
        mail = IncomingMail(
            account_id=1,
            mailbox="INBOX",
            uid="1",
            subject="Long",
            sender="sender@example.test",
            text_body="x" * 9000,
        )

        await projection.project_mail(mail, TelegramTopic(42, 99, 123))

        self.assertEqual(len(telegram.sent), 1)
        self.assertFalse(telegram.sent[0][2]["disable_notification"])
        self.assertIn("reply_markup", telegram.sent[0][2])
        self.assertIn(":123:", telegram.sent[0][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"])

    async def test_mail_projection_uses_only_llm_important_links_not_body_urls(self):
        telegram = _Telegram()
        projection = MailTelegramProjection(telegram, 42)
        mail = IncomingMail(
            account_id=1,
            mailbox="INBOX",
            uid="important-links",
            subject="Links",
            sender="sender@example.test",
            text_body=(
                "Useful https://example.test/important but also "
                "https://example.test/tracking https://example.test/logo "
                "https://example.test/footer"
            ),
            important_links=(
                {"caption": "重要入口", "link": "https://example.test/important"},
            ),
        )

        await projection.project_mail(mail, TelegramTopic(42, 99, 123))

        markup = telegram.sent[0][2]["reply_markup"]["inline_keyboard"]
        action_urls = [
            button["url"]
            for row in markup
            for button in row
            if "url" in button
        ]
        self.assertEqual(["https://example.test/important"], action_urls)

    async def test_historical_topic_delete_control_is_backfilled_once(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, thread_id = self.make_repository(directory)
            with repo.db.transaction(immediate=True) as conn:
                email_id = int(conn.execute(
                    "SELECT id FROM emails WHERE thread_id = ?", (thread_id,)
                ).fetchone()["id"])
                conn.execute(
                    """INSERT INTO telegram_delivery_parts(
                           email_id, part_index, telegram_chat_id, telegram_message_id,
                           status, created_at, updated_at
                       ) VALUES (?, 0, 42, 321, 'delivered', 1, 1)""",
                    (email_id,),
                )
                conn.execute(
                    "UPDATE emails SET body_text = ? WHERE id = ?",
                    ("Read https://example.test/unrelated and https://example.test/tracker", email_id),
                )
                conn.execute(
                    "UPDATE emails SET llm_important_links_json = ? WHERE id = ?",
                    ('[{"caption":"Renew","link":"https://example.test/renew"}]', email_id),
                )
            telegram = _Telegram()
            ui = TelegramMailBotUi(repo)
            ui.bind_client(telegram)

            self.assertEqual(await ui.backfill_topic_delete_controls(delay_seconds=0), 1)
            self.assertEqual(await ui.backfill_topic_delete_controls(delay_seconds=0), 0)

            self.assertEqual(telegram.edited_markup[0][:2], (42, 321))
            callback = telegram.edited_markup[0][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            self.assertIn(f":{thread_id}:", callback)
            action_urls = [
                button["url"]
                for row in telegram.edited_markup[0][2]["reply_markup"]["inline_keyboard"]
                for button in row
                if "url" in button
            ]
            self.assertEqual(action_urls, ["https://example.test/renew"])
            conn = repo.db.connect()
            try:
                row = conn.execute(
                    """SELECT topic_delete_markup_version, topic_delete_markup_attempts
                       FROM telegram_delivery_parts WHERE email_id = ?""",
                    (email_id,),
                ).fetchone()
                self.assertEqual((1, 1), tuple(row))
            finally:
                conn.close()

    async def test_summary_refresh_restores_llm_important_links_with_delete_control(self):
        telegram = _Telegram()
        class _SummaryRepository:
            def get_email(self, email_id):
                return {"id": int(email_id), "thread_id": 123}

            def list_telegram_delivery_parts(self, email_id):
                return [{
                    "telegram_message_id": 701,
                    "telegram_chat_id": 42,
                    "part_index": 0,
                }]

        projection = MailTelegramProjection(telegram, 42, repository=_SummaryRepository())
        mail = IncomingMail(
            account_id=1,
            mailbox="INBOX",
            uid="1",
            email_id=10,
            subject="Renewal",
            sender="sender@example.test",
            text_body="Unrelated https://example.test/tracker and https://example.test/logo",
            important_links=(
                {"caption": "Renew", "link": "https://example.test/renew"},
            ),
        )

        self.assertTrue(await projection.update_mail_summary(mail))
        markup = telegram.edited_text[0][3]["reply_markup"]["inline_keyboard"]
        self.assertEqual(markup[0][0]["text"], "🗑 删除邮件")
        self.assertEqual(markup[1][0]["url"], "https://example.test/renew")


if __name__ == "__main__":
    unittest.main()
