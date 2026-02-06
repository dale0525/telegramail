import os
import unittest


class TestEmailReceiveConfig(unittest.TestCase):
    def tearDown(self):
        for key in (
            "POLLING_INTERVAL",
            "MAIL_RECEIVE_MODE",
            "IMAP_IDLE_TIMEOUT_SECONDS",
            "IMAP_IDLE_FALLBACK_POLL_SECONDS",
            "IMAP_IDLE_RECONNECT_BACKOFF_SECONDS",
        ):
            os.environ.pop(key, None)

    def test_polling_interval_defaults_to_300_seconds(self):
        from app.cron.email_receive_config import get_polling_interval_seconds

        self.assertEqual(get_polling_interval_seconds(), 300)

    def test_polling_interval_invalid_falls_back(self):
        from app.cron.email_receive_config import get_polling_interval_seconds

        os.environ["POLLING_INTERVAL"] = "abc"
        self.assertEqual(get_polling_interval_seconds(), 300)

    def test_polling_interval_clamped_to_minimum_10_seconds(self):
        from app.cron.email_receive_config import get_polling_interval_seconds

        os.environ["POLLING_INTERVAL"] = "1"
        self.assertEqual(get_polling_interval_seconds(), 10)

    def test_receive_mode_defaults_to_hybrid(self):
        from app.cron.email_receive_config import get_mail_receive_mode

        self.assertEqual(get_mail_receive_mode(), "hybrid")

    def test_receive_mode_invalid_falls_back_to_hybrid(self):
        from app.cron.email_receive_config import get_mail_receive_mode

        os.environ["MAIL_RECEIVE_MODE"] = "invalid"
        self.assertEqual(get_mail_receive_mode(), "hybrid")

    def test_imap_idle_timeout_defaults_to_1740(self):
        from app.cron.email_receive_config import get_imap_idle_timeout_seconds

        self.assertEqual(get_imap_idle_timeout_seconds(), 1740)

    def test_imap_idle_timeout_invalid_falls_back(self):
        from app.cron.email_receive_config import get_imap_idle_timeout_seconds

        os.environ["IMAP_IDLE_TIMEOUT_SECONDS"] = "bad"
        self.assertEqual(get_imap_idle_timeout_seconds(), 1740)

    def test_fallback_poll_seconds_clamped_to_minimum_1(self):
        from app.cron.email_receive_config import get_imap_idle_fallback_poll_seconds

        os.environ["IMAP_IDLE_FALLBACK_POLL_SECONDS"] = "0"
        self.assertEqual(get_imap_idle_fallback_poll_seconds(), 1)

    def test_reconnect_backoff_seconds_clamped_to_minimum_1(self):
        from app.cron.email_receive_config import (
            get_imap_idle_reconnect_backoff_seconds,
        )

        os.environ["IMAP_IDLE_RECONNECT_BACKOFF_SECONDS"] = "-1"
        self.assertEqual(get_imap_idle_reconnect_backoff_seconds(), 1)


if __name__ == "__main__":
    unittest.main()
