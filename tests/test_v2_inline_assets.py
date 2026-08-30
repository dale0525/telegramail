from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import V2Repository
from app.integrations.mail.imap import IMAPTransport
from app.integrations.mail.types import Attachment, IncomingMail
from app.services.v2_repository_adapter import V2MailRepositoryAdapter


class InlineAssetTests(unittest.TestCase):
    def test_imap_parser_keeps_image_cid_and_ignores_non_image_cid(self) -> None:
        raw = b'''From: sender@example.test
To: me@example.test
Subject: Inline
Content-Type: multipart/related; boundary="parts"

--parts
Content-Type: text/html; charset=utf-8

<img src="cid:logo@example.test">
--parts
Content-Type: image/png
Content-ID: <logo@example.test>
Content-Disposition: inline

png-bytes
--parts
Content-Type: text/plain
Content-ID: <ignored@example.test>
Content-Disposition: inline

ignored
--parts--
'''
        parsed = IMAPTransport({"id": 1}, client_cls=lambda _: None)._parse(raw, "INBOX", "7")
        self.assertEqual(parsed.attachments[0].content_id, "<logo@example.test>")
        self.assertTrue(parsed.attachments[0].is_inline)
        self.assertEqual(parsed.attachments[0].mime_type, "image/png")
        self.assertEqual(len(parsed.attachments), 1)

    def test_adapter_persists_duplicate_retries_and_hard_delete_cleans_files(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            with mock.patch.dict(os.environ, {"TELEGRAMAIL_DATA_DIR": data_dir}, clear=False):
                self._test_adapter_storage(data_dir)

    def _test_adapter_storage(self, data_dir: str) -> None:
            repository = V2Repository(Path(data_dir) / "mail.db", master_key=b"k" * 32)
            account = repository.create_account(
                {
                    "email": "me@example.test",
                    "imap_server": "imap.example.test",
                    "imap_port": 993,
                    "imap_ssl": True,
                    "smtp_server": "smtp.example.test",
                    "smtp_port": 465,
                    "smtp_ssl": True,
                },
                "secret",
            )
            image = b"png-bytes"
            mail = IncomingMail(
                account_id=account["id"], mailbox="INBOX", uid="7", uidvalidity="9",
                html_body='<img src="cid:logo@example.test">',
                attachments=(Attachment("logo.png", image, "image/png", "<logo@example.test>", True),),
            )
            adapter = V2MailRepositoryAdapter(repository)
            self.assertTrue(adapter.insert_incoming_if_absent(mail))
            self.assertFalse(adapter.insert_incoming_if_absent(mail))
            assets = repository.list_email_inline_assets(1)
            self.assertEqual(len(assets), 1)
            path = Path(data_dir) / "inline-assets" / "1" / (hashlib.sha256(image).hexdigest() + ".bin")
            self.assertEqual(path.read_bytes(), image)
            self.assertTrue(repository.hard_delete_account(int(account["id"])))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
