from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.db import V2Repository
from app.db.repository import decode_inbox_cursor
from app.services.telegram_mail_ui import TelegramMailBotUi


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []
        self.edited: list[tuple[int, int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **extra):
        self.sent.append((chat_id, text, extra))
        return [{"message_id": 100 + len(self.sent)}]

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **extra):
        self.edited.append((chat_id, message_id, text, extra))
        return True

    async def delete_message(self, chat_id: int, message_id: int):
        return True


class InboxOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = V2Repository(Path(self.tmp.name) / "inbox.db", master_key=b"i" * 32)
        self.account = self.repo.create_account(
            {
                "email": "owner@example.test",
                "imap_server": "imap.example.test",
                "imap_port": 993,
                "smtp_server": "smtp.example.test",
                "smtp_port": 465,
            },
            "password",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add_thread(self, index: int, *, created_at: int = 100) -> int:
        row = self.repo.insert_incoming_if_absent(
            int(self.account["id"]), mailbox="INBOX", uid=str(index),
            message_id=f"<message-{index}@example.test>", sender=f"sender{index}@example.test",
            subject=f"Subject {index}", body_text=f"body {index}",
        )
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE emails SET created_at = ?, updated_at = ? WHERE id = ?", (created_at, created_at, row["id"]))
        thread = self.repo.assign_thread(row["id"], telegram_chat_id=42, telegram_message_thread_id=1000 + index)
        return int(thread["id"])

    def test_keyset_cursor_has_stable_tie_breaker_and_ignores_newer_insert(self):
        for index in range(7):
            self._add_thread(index, created_at=100)
        first = self.repo.list_telegram_inbox_threads()
        self.assertEqual([7, 6, 5, 4, 3], [item["thread_id"] for item in first["items"]])
        self.assertTrue(first["has_more"])
        self.assertEqual((100, 3), decode_inbox_cursor(first["next_cursor"]))

        self._add_thread(99, created_at=101)
        second = self.repo.list_telegram_inbox_threads(cursor=first["next_cursor"])
        self.assertEqual([2, 1], [item["thread_id"] for item in second["items"]])
        self.assertEqual({7, 6, 5, 4, 3}, {item["thread_id"] for item in first["items"]})

    def test_latest_projection_recomputes_after_tombstone(self):
        first = self._add_thread(1, created_at=100)
        second_email = self.repo.insert_incoming_if_absent(
            int(self.account["id"]), mailbox="INBOX", uid="2", message_id="<message-2@example.test>",
            subject="new", body_text="new",
        )
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE emails SET created_at = 200, updated_at = 200 WHERE id = ?", (second_email["id"],))
        self.repo.assign_thread(second_email["id"], thread_id=first)
        self.assertEqual("new", self.repo.get_telegram_inbox_thread(first)["subject"])
        self.repo.tombstone_email(second_email["id"], now=300)
        self.assertEqual("Subject 1", self.repo.get_telegram_inbox_thread(first)["subject"])

    def test_stale_latest_projection_is_repaired_before_listing(self):
        thread_id = self._add_thread(1, created_at=100)
        newer = self.repo.insert_incoming_if_absent(
            int(self.account["id"]), mailbox="INBOX", uid="2",
            message_id="<message-2@example.test>", subject="newer", body_text="newer",
        )
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE emails SET created_at = 200, updated_at = 200 WHERE id = ?", (newer["id"],))
            conn.execute("UPDATE emails SET thread_id = ? WHERE id = ?", (thread_id, newer["id"]))
            conn.execute("UPDATE mail_threads SET latest_email_id = ?, latest_at = ? WHERE id = ?", (1, 100, thread_id))
        self.assertEqual("newer", self.repo.get_telegram_inbox_thread(thread_id)["subject"])

    def test_search_ignores_tombstoned_history_but_keeps_live_messages(self):
        thread_id = self._add_thread(1)
        deleted = self.repo.insert_incoming_if_absent(
            int(self.account["id"]), mailbox="INBOX", uid="2",
            message_id="<message-2@example.test>", subject="private deleted phrase",
            body_text="private deleted phrase",
        )
        self.repo.assign_thread(deleted["id"], thread_id=thread_id)
        self.repo.tombstone_email(deleted["id"], now=300)

        self.assertEqual([], self.repo.list_telegram_inbox_threads(search="private deleted")["items"])
        self.assertEqual([thread_id], [
            item["thread_id"]
            for item in self.repo.list_telegram_inbox_threads(search="Subject 1")["items"]
        ])

    def test_legacy_offset_translation_returns_end_of_list_instead_of_latest_page(self):
        for index in range(3):
            self._add_thread(index)
        self.assertEqual([2, 1], [item["thread_id"] for item in self.repo.list_telegram_inbox_threads(offset=1)["items"]])
        self.assertEqual([], self.repo.list_telegram_inbox_threads(offset=3)["items"])
        self.assertEqual([], self.repo.list_telegram_inbox_threads(offset=5)["items"])

    def test_delete_snapshot_excludes_local_outgoing_projection_uids(self):
        thread_id = self._add_thread(1)
        self.repo.insert_outgoing_email(
            int(self.account["id"]), thread_id=thread_id,
            message_id="<sent@example.test>", sender="owner@example.test",
            recipient="recipient@example.test", subject="reply", body_text="reply",
        )
        operation = self.repo.create_delete_operation(
            int(self.account["id"]), "delete-thread-with-reply", thread_id=thread_id,
        )
        targets = self.repo.list_delete_targets(operation["id"])
        self.assertEqual(1, len(targets))
        self.assertEqual("INBOX", targets[0]["provider_mailbox"])

    def test_delivery_refresh_is_debounced_and_history_is_not_interrupted(self):
        self._add_thread(1)
        self.repo.bind_admin(42, private_chat_id=42)
        telegram = _Telegram()
        ui = TelegramMailBotUi(self.repo)
        ui.bind_client(telegram)

        async def run():
            ui.request_inbox_refresh()
            ui.request_inbox_refresh()
            await asyncio.sleep(0.9)
            self.assertEqual(1, len(telegram.sent))
            page = self.repo.list_telegram_inbox_threads()
            # Persist a historical cursor, then ensure a new refresh is ignored.
            self.repo.set_inbox_panel_state(42, cursor=page["next_cursor"] or "AAAAAGqLCb0AAAAAAAAAEA")
            ui.request_inbox_refresh()
            await asyncio.sleep(0.9)
            self.assertEqual(0, len(telegram.edited))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
