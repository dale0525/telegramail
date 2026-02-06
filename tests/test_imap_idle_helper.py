import unittest


class _FakeIdleConn:
    def __init__(self, *, capability_data=None, lines=None, tag="A001"):
        self._capability_data = capability_data or [b"IMAP4rev1 IDLE"]
        self._lines = list(lines or [])
        self._tag = tag
        self.sent = []
        self.sock = object()

    def capability(self):
        return "OK", self._capability_data

    def _new_tag(self):
        return self._tag

    def send(self, data):
        self.sent.append(data)

    def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)


def _wait_sequence(*values):
    seq = iter(values)

    def _wait(_conn, _timeout):
        return next(seq, False)

    return _wait


class TestImapIdleHelper(unittest.TestCase):
    def test_supports_idle_true(self):
        from app.email_utils.imap_idle_helper import supports_idle

        conn = _FakeIdleConn(capability_data=[b"IMAP4rev1 UIDPLUS IDLE"])
        self.assertTrue(supports_idle(conn))

    def test_supports_idle_false(self):
        from app.email_utils.imap_idle_helper import supports_idle

        conn = _FakeIdleConn(capability_data=[b"IMAP4rev1 UIDPLUS"])
        self.assertFalse(supports_idle(conn))

    def test_idle_wait_once_returns_true_on_exists(self):
        from app.email_utils.imap_idle_helper import idle_wait_once

        conn = _FakeIdleConn(
            lines=[
                b"+ idling\r\n",
                b"* 2 EXISTS\r\n",
                b"A001 OK IDLE terminated\r\n",
            ]
        )

        changed = idle_wait_once(
            conn,
            timeout_seconds=10,
            wait_for_data=_wait_sequence(True, True, True),
        )

        self.assertTrue(changed)
        self.assertEqual(conn.sent[0], b"A001 IDLE\r\n")
        self.assertIn(b"DONE\r\n", conn.sent)

    def test_idle_wait_once_returns_false_on_timeout(self):
        from app.email_utils.imap_idle_helper import idle_wait_once

        conn = _FakeIdleConn(
            lines=[
                b"+ idling\r\n",
                b"A001 OK IDLE terminated\r\n",
            ]
        )

        changed = idle_wait_once(
            conn,
            timeout_seconds=10,
            wait_for_data=_wait_sequence(True, False, True),
        )

        self.assertFalse(changed)
        self.assertIn(b"DONE\r\n", conn.sent)


if __name__ == "__main__":
    unittest.main()
