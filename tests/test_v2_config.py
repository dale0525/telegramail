"""Configuration contracts for the v2 HTTP deployment."""

from __future__ import annotations

import os
import base64
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.db.crypto import MasterKeyError, load_master_key
from app.main import _webhook_url


class SettingsTests(unittest.TestCase):
    def test_missing_session_secret_has_clear_non_sensitive_error(self) -> None:
        with patch.dict(os.environ, {"SESSION_SECRET": ""}, clear=True):
            with self.assertRaisesRegex(ValueError, "SESSION_SECRET") as error:
                Settings.from_env()

        self.assertNotIn("replace_with", str(error.exception))

    def test_short_session_secret_does_not_echo_its_value(self) -> None:
        secret = "too-short-secret"
        with patch.dict(os.environ, {"SESSION_SECRET": secret}, clear=True):
            with self.assertRaisesRegex(ValueError, "at least 32 characters") as error:
                Settings.from_env()

        self.assertNotIn(secret, str(error.exception))

    def test_valid_minimum_configuration_loads(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SESSION_SECRET": "a" * 32,
                "WEB_BASE_URL": "https://telegramail.example.test",
                "TELEGRAM_WEBHOOK_SECRET": "webhook-test-secret",
                "SETUP_CODE": "setup-test-code",
                "MASTER_KEY": base64.b64encode(b"k" * 32).decode("ascii"),
            },
            clear=True,
        ):
            settings = Settings.from_env()
            master_key = load_master_key()

        self.assertEqual(settings.session_secret, "a" * 32)
        self.assertEqual(settings.web_dist.as_posix(), "web/dist")
        self.assertEqual(settings.web_base_url, "https://telegramail.example.test")
        self.assertEqual(settings.webhook_secret, "webhook-test-secret")
        self.assertEqual(settings.setup_code, "setup-test-code")
        self.assertEqual(master_key, b"k" * 32)
        self.assertFalse(hasattr(settings, "master_key_file"))

    def test_webhook_url_is_always_derived_from_web_base_url(self) -> None:
        settings = Settings(session_secret="s" * 32, web_base_url="https://telegramail.example.test/")
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_URL": "https://untrusted.example/webhook"}):
            self.assertEqual(
                _webhook_url(settings),
                "https://telegramail.example.test/api/v1/telegram/webhook",
            )

    def test_legacy_master_key_file_remains_a_fallback(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "master.key"
            legacy.write_bytes(b"f" * 32)
            with patch.dict(os.environ, {"MASTER_KEY_FILE": str(legacy)}, clear=True):
                self.assertEqual(load_master_key(), b"f" * 32)

    def test_direct_master_key_precedes_legacy_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "master.key"
            legacy.write_bytes(b"f" * 32)
            direct = base64.b64encode(b"d" * 32).decode("ascii")
            with patch.dict(os.environ, {"MASTER_KEY": direct, "MASTER_KEY_FILE": str(legacy)}, clear=True):
                self.assertEqual(load_master_key(), b"d" * 32)

    def test_direct_master_key_must_be_base64_not_raw_text(self) -> None:
        with patch.dict(os.environ, {"MASTER_KEY": "x" * 32}, clear=True):
            with self.assertRaises(MasterKeyError):
                load_master_key()
