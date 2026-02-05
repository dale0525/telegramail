import unittest


class _FakeConn:
    def __init__(self, list_lines):
        self._list_lines = list_lines
        self.calls = []

    def list(self):
        self.calls.append(("list",))
        return "OK", self._list_lines

    def logout(self):
        self.calls.append(("logout",))


class TestImapListMailboxes(unittest.TestCase):
    def test_list_mailboxes_parses_common_formats_and_filters_noselect(self):
        from app.email_utils.imap_client import IMAPClient

        client = IMAPClient(account={"id": 1, "email": "a@example.com"})
        client.conn = _FakeConn(
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren \\Noselect) "/" "Archive"',
                b'(\\HasChildren) "/" "Projects"',
                b'(\\HasNoChildren) NIL "Sent Items"',
                b'(\\HasNoChildren) / Drafts',
            ]
        )

        all_boxes = client.list_mailboxes(selectable_only=False)
        self.assertTrue(any(b["name"] == "INBOX" for b in all_boxes))
        self.assertTrue(any(b["name"] == "Archive" for b in all_boxes))
        self.assertTrue(any(b["name"] == "Sent Items" for b in all_boxes))
        self.assertTrue(any(b["name"] == "Drafts" for b in all_boxes))

        selectable = client.list_mailboxes(selectable_only=True)
        self.assertTrue(any(b["name"] == "INBOX" for b in selectable))
        self.assertFalse(any(b["name"] == "Archive" for b in selectable))

