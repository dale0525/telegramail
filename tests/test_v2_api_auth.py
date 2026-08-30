"""Security contracts for the v2 Mini App HTTP boundary."""

from __future__ import annotations

import asyncio
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
from app.core.security import AuthenticationError, verify_telegram_init_data
from app.db import V2Repository


BOT_TOKEN = "123456:test-token"


def signed_init_data(*, user_id: int = 42, auth_date: int | None = None) -> str:
    values = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class _OneTimeSetupStore:
    def __init__(self) -> None:
        self.used = False

    def consume_setup_code(self, *, code: str, telegram_user_id: int) -> bool:
        if code == "one-time" and telegram_user_id == 42 and not self.used:
            self.used = True
            return True
        return False

    def list_accounts(self):
        return []


class InitDataTests(unittest.TestCase):
    def test_tampered_and_expired_init_data_are_rejected(self) -> None:
        valid = signed_init_data()
        with self.assertRaises(AuthenticationError):
            verify_telegram_init_data(valid + "tampered", bot_token=BOT_TOKEN, max_age_seconds=300)
        with self.assertRaises(AuthenticationError):
            verify_telegram_init_data(
                signed_init_data(auth_date=int(time.time()) - 301),
                bot_token=BOT_TOKEN,
                max_age_seconds=300,
            )


class ApiSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_secure_setup_cookie_is_available_inside_telegram_web_iframe(self) -> None:
        app = create_app(
            Settings(session_secret="s" * 32, telegram_bot_token=BOT_TOKEN, secure_cookies=True, web_dist=None),
            db=_OneTimeSetupStore(),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
            setup = await client.post("/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"})
        self.assertEqual(setup.status_code, 200)
        cookie = setup.headers["set-cookie"].lower()
        self.assertIn("secure", cookie)
        self.assertIn("samesite=none", cookie)

    async def test_setup_is_one_time_and_cookie_writes_require_csrf(self) -> None:
        app = create_app(
            Settings(session_secret="s" * 32, telegram_bot_token=BOT_TOKEN, secure_cookies=False, web_dist=None),
            db=_OneTimeSetupStore(),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            setup = await client.post("/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"})
            self.assertEqual(setup.status_code, 200)
            csrf = setup.json()["csrf_token"]
            self.assertEqual((await client.post("/api/v1/accounts", json={"email": "a@example.test"})).status_code, 403)
            self.assertEqual(
                (await client.post("/api/v1/accounts", headers={"X-CSRF-Token": csrf}, json={"email": "a@example.test"})).status_code,
                422,
            )
            self.assertEqual(
                (await client.post("/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"})).status_code,
                401,
            )

    async def test_account_secret_is_not_serialized_and_send_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = V2Repository(Path(directory) / "api.db", master_key=b"k" * 32)
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
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                setup = await client.post("/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"})
                csrf_headers = {"X-CSRF-Token": setup.json()["csrf_token"]}
                created = await client.post(
                    "/api/v1/accounts",
                    headers=csrf_headers,
                    json={
                        "email": "a@example.test",
                        "password": "never-return-this",
                        "imap_server": "imap.example.test",
                        "imap_port": 993,
                        "smtp_server": "smtp.example.test",
                        "smtp_port": 465,
                    },
                )
                self.assertEqual(created.status_code, 201)
                self.assertNotIn("never-return-this", created.text)
                self.assertTrue(created.json()["credential_configured"])
                binding = repo.get_admin_binding()
                self.assertEqual(binding["telegram_user_id"], 42)
                self.assertEqual(binding["private_chat_id"], 42)
                draft = await client.post("/api/v1/drafts", headers=csrf_headers, json={"account_id": created.json()["id"]})
                draft_id = draft.json()["id"]
                first = await client.post(
                    f"/api/v1/drafts/{draft_id}/send",
                    headers={**csrf_headers, "Idempotency-Key": "stable-key"},
                )
                second = await client.post(
                    f"/api/v1/drafts/{draft_id}/send",
                    headers={**csrf_headers, "Idempotency-Key": "stable-key"},
                )
                self.assertEqual(first.status_code, 202)
                self.assertEqual(second.status_code, 202)
                self.assertEqual(first.json()["id"], second.json()["id"])

    async def test_bound_admin_can_create_session_on_a_new_device_without_setup_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = V2Repository(Path(directory) / "api.db", master_key=b"k" * 32)
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
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as first_device:
                initial = await first_device.post(
                    "/api/v1/auth/setup",
                    json={"initData": signed_init_data(user_id=42), "code": "one-time"},
                )
            self.assertEqual(initial.status_code, 200)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as new_device:
                renewed = await new_device.post(
                    "/api/v1/auth/session",
                    json={"initData": signed_init_data(user_id=42)},
                )
                forged = await new_device.post(
                    "/api/v1/auth/session",
                    json={"initData": signed_init_data(user_id=42) + "tampered"},
                )
                other_user = await new_device.post(
                    "/api/v1/auth/session",
                    json={"initData": signed_init_data(user_id=7)},
                )
                other_setup = await new_device.post(
                    "/api/v1/auth/setup",
                    json={"initData": signed_init_data(user_id=7), "code": "one-time"},
                )
            self.assertEqual(renewed.status_code, 200)
            self.assertIn("telegramail_session", renewed.headers["set-cookie"])
            self.assertEqual(forged.status_code, 401)
            self.assertEqual(other_user.status_code, 403)
            self.assertEqual(other_setup.status_code, 403)

    async def test_thread_messages_expose_cc_for_reply_all(self) -> None:
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
                "secret",
            )
            now = int(time.time())
            with repo.db.transaction(immediate=True) as connection:
                thread_id = int(connection.execute(
                    "INSERT INTO mail_threads(account_id, subject_normalized, root_message_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (int(account["id"]), "reply all", "<root@example.test>", now, now),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO emails(account_id, thread_id, mailbox, uid, sender, recipient, cc, subject, email_date, body_text, created_at, updated_at)
                       VALUES (?, ?, 'INBOX', '1', ?, ?, ?, 'Reply all', ?, 'Hello', ?, ?)""",
                    (
                        int(account["id"]), thread_id, "sender@example.test",
                        "owner@example.test, other@example.test", "copy@example.test",
                        str(now), now, now,
                    ),
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
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                setup = await client.post(
                    "/api/v1/auth/setup",
                    json={"initData": signed_init_data(), "code": "one-time"},
                )
                self.assertEqual(setup.status_code, 200)
                messages = await client.get(f"/api/v1/threads/{thread_id}/messages")
            self.assertEqual(messages.status_code, 200)
            self.assertEqual(messages.json()[0]["cc"], "copy@example.test")

    async def test_setup_binds_private_chat_and_replays_waiting_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = V2Repository(Path(directory) / "api.db", master_key=b"k" * 32)
            # This models mail ingested after /start established the administrator
            # identity but before the Mini App bound its private projection chat.
            repo.bind_admin(42)
            account = repo.create_account(
                {
                    "email": "a@example.test",
                    "imap_server": "imap.example.test",
                    "imap_port": 993,
                    "smtp_server": "smtp.example.test",
                    "smtp_port": 465,
                },
                "secret",
            )
            email = repo.insert_incoming_if_absent(int(account["id"]), mailbox="INBOX", uid="1")
            waiting = repo.mark_projection_waiting(int(email["id"]))
            self.assertEqual(waiting["status"], "waiting")

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
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/auth/setup",
                    json={"initData": signed_init_data(), "code": "one-time"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(repo.get_admin_binding()["private_chat_id"], 42)
            replayed = repo.scan_pending_projections(include_waiting=True)
            self.assertEqual(len(replayed), 1)
            self.assertEqual(replayed[0]["status"], "queued")
            self.assertEqual(replayed[0]["telegram_chat_id"], 42)


if __name__ == "__main__":
    unittest.main()
