import unittest
from unittest import mock


class _FakeConn:
    def __init__(self):
        self.calls = []

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
