import unittest
from unittest import mock


class TestEmailIngestion(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_account_emails_returns_count(self):
        from app.cron import email_ingestion

        account = {"id": 1, "email": "a@example.com"}
        fake_client = mock.Mock()
        fake_client.fetch_unread_emails = mock.AsyncMock(return_value=2)

        with mock.patch.object(
            email_ingestion, "IMAPClient", return_value=fake_client
        ):
            count = await email_ingestion.fetch_account_emails(account)

        self.assertEqual(count, 2)

    async def test_fetch_account_emails_safe_returns_error_tuple(self):
        from app.cron import email_ingestion

        account = {"id": 1, "email": "a@example.com"}
        fake_client = mock.Mock()
        fake_client.fetch_unread_emails = mock.AsyncMock(
            side_effect=RuntimeError("boom")
        )

        with mock.patch.object(
            email_ingestion, "IMAPClient", return_value=fake_client
        ):
            email_addr, count, error = await email_ingestion.fetch_account_emails_safe(
                account
            )

        self.assertEqual(email_addr, "a@example.com")
        self.assertEqual(count, 0)
        self.assertIn("boom", error)


if __name__ == "__main__":
    unittest.main()
