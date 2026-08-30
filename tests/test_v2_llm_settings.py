"""Persistence and HTTP contracts for global LLM settings and summaries."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api import create_app
from app.core.config import Settings
from app.db import V2Repository
from tests.test_v2_api_auth import BOT_TOKEN, signed_init_data


class LLMSettingsRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = V2Repository(Path(self.temporary.name) / "settings.db", master_key=b"k" * 32)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_api_key_is_encrypted_and_blank_updates_preserve_it(self) -> None:
        saved = self.repo.update_llm_settings({
            "enabled": True,
            "base_url": "https://llm.example.test/v1",
            "model": "small-model",
            "default_language": "zh_CN",
            "summary_threshold": 600,
            "api_key": "secret-api-key",
        })
        self.assertTrue(saved["api_key_configured"])
        self.assertEqual("zh_CN", self.repo.get_global_llm_settings()["default_language"])
        self.assertNotIn("api_key", saved)
        self.assertEqual("secret-api-key", self.repo.get_llm_settings_secret())
        updated = self.repo.update_llm_settings({"model": "next-model", "api_key": ""})
        self.assertEqual("next-model", updated["model"])
        self.assertEqual("secret-api-key", self.repo.get_llm_settings_secret())
        with self.repo.db.connect() as conn:
            raw = conn.execute("SELECT api_key_nonce, api_key_ciphertext FROM llm_settings").fetchone()
            self.assertNotIn(b"secret-api-key", bytes(raw["api_key_ciphertext"]))

    def test_summary_tasks_are_leased_and_mirrored_to_email(self) -> None:
        account = self.repo.create_account(
            {
                "email": "a@example.test", "imap_server": "imap.example.test", "imap_port": 993,
                "smtp_server": "smtp.example.test", "smtp_port": 465,
            },
            "mail-password",
        )
        email = self.repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="1", subject="Summary")
        queued = self.repo.enqueue_summary_task(email["id"])
        self.assertEqual("queued", queued["status"])
        claimed = self.repo.claim_summary_task(lease_seconds=60)
        self.assertEqual((email["id"], "running", 1), (claimed["email_id"], claimed["status"], claimed["attempts"]))
        finished = self.repo.complete_summary_task(email["id"], lease_token=claimed["lease_token"])
        self.assertEqual("completed", finished["status"])
        self.assertEqual("completed", self.repo.get_email(email["id"])["summary_status"])


class LLMSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = V2Repository(Path(self.temporary.name) / "api.db", master_key=b"k" * 32)
        self.app = create_app(
            Settings(
                session_secret="s" * 32,
                telegram_bot_token=BOT_TOKEN,
                setup_code="one-time",
                secure_cookies=False,
                web_dist=None,
            ),
            db=self.repo,
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        setup = await self.client.post(
            "/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"}
        )
        self.csrf = setup.json()["csrf_token"]

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    async def test_settings_are_authenticated_csrf_protected_and_never_echo_key(self) -> None:
        denied = await self.client.put("/api/v1/settings/llm", json={"model": "m"})
        self.assertEqual(403, denied.status_code)
        saved = await self.client.put(
            "/api/v1/settings/llm",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "enabled": True,
                "base_url": "https://llm.example.test/v1",
                "model": "m",
                "default_language": "zh_CN",
                "summary_threshold": 400,
                "api_key": "never-return-this",
            },
        )
        self.assertEqual(200, saved.status_code)
        self.assertNotIn("never-return-this", saved.text)
        self.assertTrue(saved.json()["api_key_configured"])
        read = await self.client.get("/api/v1/settings/llm")
        self.assertEqual("m", read.json()["model"])
        self.assertEqual("zh_CN", read.json()["default_language"])
        self.assertNotIn("api_key", read.json())

    async def test_connection_probe_uses_stream_and_records_status(self) -> None:
        await self.client.put(
            "/api/v1/settings/llm",
            headers={"X-CSRF-Token": self.csrf},
            json={"base_url": "https://llm.example.test/v1", "model": "m", "api_key": "secret"},
        )
        calls: list[dict] = []

        class _Stream:
            def __iter__(self):
                return iter([object()])

            def close(self):
                return None

        class _Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return _Stream()

        class _OpenAI:
            def __init__(self, **_kwargs):
                self.chat = type("Chat", (), {"completions": _Completions()})()

        with patch("openai.OpenAI", _OpenAI):
            response = await self.client.post(
                "/api/v1/settings/llm/test",
                headers={"X-CSRF-Token": self.csrf},
                json={},
            )
        self.assertEqual(True, response.json()["ok"])
        self.assertEqual("ok", response.json()["status"])
        self.assertEqual("ok", response.json()["last_test_status"])
        self.assertIsNone(response.json()["error"])
        self.assertTrue(calls[0]["stream"])
        self.assertEqual("ok", self.repo.get_llm_settings()["last_test_status"])


if __name__ == "__main__":
    unittest.main()
