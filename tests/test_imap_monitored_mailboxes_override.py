import os
import unittest


class TestImapMonitoredMailboxesOverride(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("TELEGRAMAIL_IMAP_MONITORED_MAILBOXES", None)

    def test_account_override_takes_precedence_over_env(self):
        from app.email_utils.imap_client import IMAPClient

        os.environ["TELEGRAMAIL_IMAP_MONITORED_MAILBOXES"] = "INBOX,Archive"
        client = IMAPClient(
            account={
                "id": 1,
                "email": "a@example.com",
                "imap_monitored_mailboxes": "Spam",
            }
        )

        self.assertEqual(client._get_monitored_mailboxes(), ["Spam"])

    def test_env_is_used_when_account_override_missing(self):
        from app.email_utils.imap_client import IMAPClient

        os.environ["TELEGRAMAIL_IMAP_MONITORED_MAILBOXES"] = "INBOX,Archive"
        client = IMAPClient(account={"id": 1, "email": "a@example.com"})
        self.assertEqual(client._get_monitored_mailboxes(), ["INBOX", "Archive"])

    def test_defaults_to_inbox_when_both_missing(self):
        from app.email_utils.imap_client import IMAPClient

        client = IMAPClient(account={"id": 1, "email": "a@example.com"})
        self.assertEqual(client._get_monitored_mailboxes(), ["INBOX"])

