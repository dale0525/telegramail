import unittest
from unittest import mock


class _FakeConn:
    def __init__(self):
        self.calls = []

    def list(self):
        self.calls.append(("list",))
        return "OK", [b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def select(self, mailbox):
        self.calls.append(("select", mailbox))
        return "OK", [b""]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "STORE":
            return "OK", [b"FLAGS (\\Deleted)"]
        if command == "EXPUNGE":
            return "OK", [b"1 EXPUNGE"]
        if command == "SEARCH":
            # Message exists
            if len(args) >= 4 and str(args[1]).upper() == "HEADER":
                return "OK", [b"999"]
            return "OK", [b"123"]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge",))
        return "OK", [b"1 EXPUNGE"]

    def logout(self):
        self.calls.append(("logout",))


class TestImapDelete(unittest.TestCase):
    def test_delete_email_by_uid_expunges(self):
        from app.email_utils.imap_client import IMAPClient

        fake_db = mock.Mock()
        fake_db.delete_email_by_uid.return_value = True

        with mock.patch("app.email_utils.imap_client.DBManager", return_value=fake_db):
            client = IMAPClient(account={"id": 1, "email": "test@example.com"})

        fake_conn = _FakeConn()
        client.conn = fake_conn

        ok = client.delete_email_by_uid("42")
        self.assertTrue(ok)

        # Must expunge (either UID EXPUNGE or EXPUNGE fallback).
        uid_cmds = [c for c in fake_conn.calls if c[:2] == ("uid", "EXPUNGE")]
        expunge_calls = [c for c in fake_conn.calls if c[0] == "expunge"]
        self.assertTrue(uid_cmds or expunge_calls)

    def test_delete_outgoing_email_by_message_id_searches_sent_and_expunges(self):
        from app.email_utils.imap_client import IMAPClient

        fake_db = mock.Mock()
        fake_db.delete_email_by_uid.return_value = True

        with mock.patch("app.email_utils.imap_client.DBManager", return_value=fake_db):
            client = IMAPClient(account={"id": 1, "email": "test@example.com"})

        fake_conn = _FakeConn()
        client.conn = fake_conn

        ok = client.delete_outgoing_email_by_message_id("<m1@example.com>")
        self.assertTrue(ok)

        # Select the Sent mailbox (resolved via LIST \\Sent).
        self.assertIn(("select", "Sent"), fake_conn.calls)

        # Search by Message-ID header in Sent.
        header_search = [
            c
            for c in fake_conn.calls
            if c[:2] == ("uid", "SEARCH") and "HEADER" in [str(x).upper() for x in c]
        ]
        self.assertTrue(header_search)

        # Must expunge (either UID EXPUNGE or EXPUNGE fallback).
        uid_cmds = [c for c in fake_conn.calls if c[:2] == ("uid", "EXPUNGE")]
        expunge_calls = [c for c in fake_conn.calls if c[0] == "expunge"]
        self.assertTrue(uid_cmds or expunge_calls)

        fake_db.delete_email_by_uid.assert_called_once_with(
            {"id": 1, "email": "test@example.com"}, "outgoing:<m1@example.com>"
        )
