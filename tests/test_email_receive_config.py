import os
import unittest


class TestEmailReceiveConfig(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("POLLING_INTERVAL", None)

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

if __name__ == "__main__":
    unittest.main()
