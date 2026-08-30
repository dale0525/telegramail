"""Thread-delete API contracts: complete provider snapshot and local tombstone."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.api import create_app
from app.core.config import Settings
from app.db import V2Repository


BOT_TOKEN = "123456:delete-api-test"


def signed_init_data() -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "delete-test",
        "user": json.dumps({"id": 42, "first_name": "Delete"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class ThreadDeleteApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_snapshots_every_live_message_and_tombstones_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = V2Repository(Path(directory) / "api.db", master_key=b"k" * 32)
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
            with repo.db.transaction(immediate=True) as conn:
                thread_id = int(conn.execute(
                    """INSERT INTO mail_threads(account_id, subject_normalized, telegram_chat_id,
                       telegram_message_thread_id, created_at, updated_at)
                       VALUES (?, 'thread', 42, 99, ?, ?)""",
                    (int(account["id"]), 1, 1),
                ).lastrowid)
                for uid, tombstoned in (("101", None), ("102", None), ("103", 1)):
                    conn.execute(
                        """INSERT INTO emails(account_id, thread_id, mailbox, uid, sender, recipient,
                           created_at, updated_at, tombstoned_at)
                           VALUES (?, ?, 'INBOX', ?, 'a@example.test', 'owner@example.test', ?, ?, ?)""",
                        (int(account["id"]), thread_id, uid, 1, 1, tombstoned),
                    )

            app = create_app(
                Settings(
                    session_secret="s" * 32,
                    telegram_bot_token=BOT_TOKEN,
                    setup_code="one-time",
                    secure_cookies=False,
                    web_dist=None,
                ),
                db=repo,
            )
            class RuntimeWake:
                calls = 0

                def wake(self):
                    self.calls += 1

            wake = RuntimeWake()
            app.state.worker_runtime = wake
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                setup = await client.post("/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"})
                self.assertEqual(setup.status_code, 200)
                headers = {"X-CSRF-Token": setup.json()["csrf_token"], "Idempotency-Key": "whole-thread"}
                accepted = await client.delete(f"/api/v1/threads/{thread_id}", headers=headers)
                duplicate = await client.delete(f"/api/v1/threads/{thread_id}", headers=headers)
                before_tombstone = await client.get(f"/api/v1/threads/{thread_id}/messages")
                pending_list = await client.get("/api/v1/threads")
                session_cookie = setup.cookies.get("telegramail_session")

            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertEqual(duplicate.status_code, 202, duplicate.text)
            self.assertEqual(wake.calls, 2)
            self.assertEqual(accepted.json()["id"], duplicate.json()["id"])
            self.assertEqual(before_tombstone.status_code, 200)
            self.assertEqual(pending_list.status_code, 200)
            self.assertNotIn(str(thread_id), [row["id"] for row in pending_list.json()])
            # UID 103 was tombstoned before the API call and is never exposed.
            self.assertEqual(len(before_tombstone.json()), 2)
            operation_id = int(accepted.json()["id"].split(":", 1)[1])
            target = repo.get_delete_target(operation_id)
            self.assertEqual(
                [(item["provider_mailbox"], item["provider_uid"]) for item in target["provider_mappings"]],
                [("INBOX", "101"), ("INBOX", "102")],
            )
            self.assertEqual(target["topic"], {"telegram_chat_id": 42, "telegram_message_thread_id": 99})

            # The saga may only tombstone after provider and Topic phases succeed.
            repo.update_delete(operation_id, status="deleting", provider_deleted=True)
            repo.update_delete(operation_id, status="deleting", topic_deleted=True)
            final = repo.tombstone_thread(thread_id, delete_operation_id=operation_id)
            self.assertEqual(final["status"], "deleted")
            self.assertTrue(final["tombstoned"])
            conn = repo.db.connect()
            try:
                self.assertEqual(conn.execute("SELECT status FROM mail_threads WHERE id = ?", (thread_id,)).fetchone()[0], "tombstoned")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM emails WHERE thread_id = ? AND tombstoned_at IS NULL", (thread_id,)).fetchone()[0], 0)
            finally:
                conn.close()

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                client.cookies.set("telegramail_session", session_cookie)
                listed = await client.get("/api/v1/threads")
                deleted_detail = await client.get(f"/api/v1/threads/{thread_id}/messages")
            self.assertEqual(listed.status_code, 200)
            self.assertNotIn(str(thread_id), [row["id"] for row in listed.json()])
            self.assertEqual(deleted_detail.status_code, 404)


if __name__ == "__main__":
    unittest.main()
