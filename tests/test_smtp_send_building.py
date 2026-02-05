import unittest
from email import message_from_string
from unittest import mock


class _FakeSMTP:
    def __init__(self, *args, **kwargs):
        self.sent = []

    def ehlo(self):
        return None

    def login(self, username, password):
        return None

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append((from_addr, list(to_addrs), msg))
        return {}

    def quit(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.quit()
        return False


class TestSmtpClient(unittest.TestCase):
    def test_builds_multipart_alternative_with_headers(self):
        from app.email_utils.smtp_client import build_email_message

        msg = build_email_message(
            from_email="b@example.com",
            from_name="Work",
            to_addrs=["to@example.com"],
            cc_addrs=["cc@example.com"],
            subject="Hello",
            text_body="plain",
            html_body="<p>html</p>",
            reply_to="b@example.com",
            in_reply_to="<m1@example.com>",
            references=["<r1@example.com>", "<r2@example.com>"],
        )

        self.assertEqual(msg["From"], "Work <b@example.com>")
        self.assertEqual(msg["To"], "to@example.com")
        self.assertEqual(msg["Cc"], "cc@example.com")
        self.assertNotIn("Bcc", msg)
        self.assertEqual(msg["Reply-To"], "b@example.com")
        self.assertEqual(msg["In-Reply-To"], "<m1@example.com>")
        self.assertEqual(msg["References"], "<r1@example.com> <r2@example.com>")
        self.assertTrue(msg.is_multipart())

    def test_chunks_large_bcc_without_dup_to_cc(self):
        from app.email_utils.smtp_client import SMTPClient

        fake = _FakeSMTP()
        with mock.patch("smtplib.SMTP_SSL", return_value=fake):
            client = SMTPClient(
                server="smtp.example.com",
                port=465,
                username="a@example.com",
                password="pw",
                use_ssl=True,
                max_recipients_per_email=2,
            )

            client.send_email_sync(
                from_email="b@example.com",
                from_name="Work",
                to_addrs=["to@example.com"],
                cc_addrs=["cc@example.com"],
                bcc_addrs=["b1@example.com", "b2@example.com", "b3@example.com"],
                subject="Hello",
                text_body="plain",
                html_body="<p>html</p>",
            )

        # First: To/Cc only (no bcc), then bcc chunks of size 2 and 1.
        self.assertEqual(len(fake.sent), 3)

        # 1) To/Cc only
        _from, rcpt1, msg1 = fake.sent[0]
        self.assertEqual(set(rcpt1), {"to@example.com", "cc@example.com"})
        parsed1 = message_from_string(msg1)
        self.assertEqual(parsed1["To"], "to@example.com")
        self.assertEqual(parsed1["Cc"], "cc@example.com")
        self.assertIsNone(parsed1.get("Bcc"))

        # 2) Bcc chunk 1 (2 recipients)
        _from, rcpt2, msg2 = fake.sent[1]
        self.assertEqual(set(rcpt2), {"b1@example.com", "b2@example.com"})
        parsed2 = message_from_string(msg2)
        self.assertEqual(parsed2["To"], "b@example.com")
        self.assertIsNone(parsed2.get("Cc"))
        self.assertIsNone(parsed2.get("Bcc"))

        # 3) Bcc chunk 2 (1 recipient)
        _from, rcpt3, msg3 = fake.sent[2]
        self.assertEqual(set(rcpt3), {"b3@example.com"})
        parsed3 = message_from_string(msg3)
        self.assertEqual(parsed3["To"], "b@example.com")
        self.assertIsNone(parsed3.get("Cc"))
        self.assertIsNone(parsed3.get("Bcc"))

    def test_builds_multipart_with_attachments(self):
        from app.email_utils.smtp_client import build_email_message

        msg = build_email_message(
            from_email="b@example.com",
            from_name="Work",
            to_addrs=["to@example.com"],
            subject="Hello",
            text_body="plain",
            html_body="<p>html</p>",
            attachments=[
                {"filename": "a.txt", "mime_type": "text/plain", "data": b"abc"},
            ],
        )

        raw = msg.as_string()
        parsed = message_from_string(raw)
        self.assertTrue(parsed.is_multipart())
        # There should be a part with attachment disposition.
        attachments = [
            p
            for p in parsed.walk()
            if p.get_content_disposition() == "attachment"
        ]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "a.txt")
