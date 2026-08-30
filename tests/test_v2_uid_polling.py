import os
import smtplib
import tempfile
import unittest
from unittest import mock

from app.integrations.mail import FetchedMessages, IncomingMail, MailDraft, MailTelegramProjection, SMTPTransport, TelegramTopic
from app.integrations.mail.imap import IMAPTransport
from app.services import InMemoryMailRepository, V2MailRepositoryAdapter
from app.services import MailService
from app.workers import MailIngestionWorker, MailOutboxWorker, MailProjectionWorker
from app.workers.runtime import MailWorkerRuntime


class _UIDConnection:
    def __init__(self):
        self.calls = []

    def select(self, mailbox):
        self.calls.append(("select", mailbox))
        return "OK", [b"2"]

    def response(self, code):
        self.calls.append(("response", code))
        if code == "UIDVALIDITY":
            return "UIDVALIDITY", [b"777"]
        return None, []

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"42 43"]
        if command == "FETCH":
            uid = args[0]
            raw = f"From: sender@example.com\nTo: me@example.com\nSubject: UID {uid}\n\nbody".encode()
            return "OK", [(f"{uid} (BODY[]".encode(), raw), b")"]
        if command == "STORE":
            return "OK", []
        raise AssertionError(command)


class _IMAPClient:
    def __init__(self, account): self.conn = _UIDConnection()
    def connect(self): return True
    def disconnect(self): pass


class TestUidPolling(unittest.IsolatedAsyncioTestCase):
    async def test_default_transport_never_creates_legacy_database(self):
        class Conn:
            capabilities = (b"UIDPLUS",)
            def select(self, mailbox): return "OK", [b"[UIDVALIDITY 1]"]
            def uid(self, command, *args):
                if command == "SEARCH": return "OK", [b""]
                return "OK", []
            def logout(self): pass

        with tempfile.TemporaryDirectory(prefix="telegramail-v2-imap-") as data_dir:
            account = {"id": 1, "email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                       "imap_ssl": True, "password": "secret"}
            with mock.patch.dict(os.environ, {"TELEGRAMAIL_DATA_DIR": data_dir,
                                               "TELEGRAMAIL_DB_PATH": os.path.join(data_dir, "telegramail.db")}, clear=False), \
                 mock.patch("app.email_utils.imap_connection.ConnectionFactory.try_imap_connection", return_value=(True, "", Conn())):
                transport = IMAPTransport(account)
                self.assertEqual((await transport.fetch_incremental()).messages, ())
                self.assertTrue(await transport.verify())
                self.assertTrue(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
            self.assertFalse(os.path.exists(os.path.join(data_dir, "telegramail.db")))

    async def test_transport_searches_uid_range_not_unseen(self):
        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=_IMAPClient)
        batch = await transport.fetch_incremental("INBOX", after_uid=41)
        self.assertEqual([m.uid for m in batch.messages], ["42", "43"])
        self.assertEqual(batch.uidvalidity, "777")
        calls = transport.client.conn.calls
        self.assertIn(("SEARCH", None, "UID", "42:*"), calls)
        self.assertFalse(any(call[0] == "search" for call in calls))

    async def test_transport_does_not_treat_select_exists_count_as_uidvalidity(self):
        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=_IMAPClient)
        transport.client.conn.response = lambda _: (None, [])
        batch = await transport.fetch_incremental("INBOX", after_uid=41)
        self.assertIsNone(batch.uidvalidity)

    async def test_transport_marks_exact_uid_seen_and_checks_uidvalidity(self):
        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=_IMAPClient)
        self.assertTrue(await transport.mark_read("INBOX", "42", uidvalidity="777"))
        self.assertIn(("STORE", "42", "+FLAGS.SILENT", r"(\Seen)"), transport.client.conn.calls)

    async def test_transport_refuses_to_mark_reused_uid_after_uidvalidity_change(self):
        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=_IMAPClient)
        with self.assertRaisesRegex(RuntimeError, "UIDVALIDITY changed"):
            await transport.mark_read("INBOX", "42", uidvalidity="999")
        self.assertNotIn(("STORE", "42", "+FLAGS.SILENT", r"(\Seen)"), transport.client.conn.calls)

    async def test_worker_uses_durable_max_uid_and_keeps_uid_dedupe(self):
        store = InMemoryMailRepository()
        store.insert_incoming_if_absent(IncomingMail(account_id=1, mailbox="INBOX", uid="41"))

        class Client:
            def __init__(self): self.after = None
            async def fetch_messages(self, mailbox, *, after_uid=0):
                self.after = after_uid
                return [IncomingMail(account_id=1, mailbox=mailbox, uid="42", subject="new")]

        client = Client()

        class Telegram:
            async def ensure_private_topic(self, account, subject): return 9
            async def project_mail(self, mail, topic): return True

        worker = MailIngestionWorker(store, lambda _: client, Telegram())
        self.assertEqual(await worker.ingest_account({"id": 1}), 1)
        self.assertEqual(client.after, 41)
        self.assertEqual(store.max_ingested_uid(1, "INBOX"), 42)

    async def test_uidvalidity_change_resets_then_rescans_from_uid_one(self):
        store = InMemoryMailRepository()
        store.reset_imap_cursor(1, "INBOX", "old", 10)

        class Client:
            def __init__(self): self.after = []
            async def fetch_incremental(self, mailbox, *, after_uid=0):
                self.after.append(after_uid)
                if after_uid:
                    return FetchedMessages((), "new")
                return FetchedMessages((IncomingMail(account_id=1, mailbox=mailbox, uid="1", subject="reset"),), "new")

        class Telegram:
            async def ensure_private_topic(self, account, subject): return 7
            async def project_mail(self, mail, topic): return True

        client = Client()
        self.assertEqual(await MailIngestionWorker(store, lambda _: client, Telegram()).ingest_account({"id": 1}), 1)
        self.assertEqual(client.after, [10, 0])
        self.assertEqual(store.get_imap_cursor(1, "INBOX"), {"uidvalidity": "new", "last_uid": 1, "reset_required": False})

    async def test_projection_uses_escaped_plain_text_card_not_email_html(self):
        class Telegram:
            def __init__(self): self.calls = []
            async def send_message(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return [{"message_id": len(self.calls)}]
        client = Telegram()
        projection = MailTelegramProjection(client, 1)
        await projection.project_mail(IncomingMail(account_id=1, mailbox="INBOX", uid="1", sender="<b>sender</b>", subject="<img>",
                                                    text_body="safe <text>", html_body="<script>must not be sent</script>"), TelegramTopic(1, 2))
        sent = "\n".join(args[1] for args, _ in client.calls)
        self.assertIn("&lt;text&gt;", sent)
        self.assertNotIn("<script>", sent)
        self.assertEqual(client.calls[0][1]["parse_mode"], "HTML")

    async def test_enabled_llm_labels_are_persisted_without_blocking_ingestion(self):
        store = InMemoryMailRepository()

        class Client:
            async def fetch_messages(self, mailbox, *, after_uid=0):
                return [IncomingMail(account_id=1, mailbox=mailbox, uid="1", text_body="body")]

        class RawTelegram:
            def __init__(self): self.text = ""
            async def create_forum_topic(self, chat, subject): return {"message_thread_id": 7}
            async def send_message(self, chat, text, **kwargs):
                self.text += text
                return [{"message_id": 1}]

        def summarize(body):
            self.assertEqual(body, "body")
            return {"category": "task", "priority": "high", "category_confidence": 0.8, "summary": "safe summary"}

        raw = RawTelegram()
        telegram = MailTelegramProjection(raw, 1)
        self.assertEqual(await MailIngestionWorker(store, lambda _: Client(), telegram, llm_summarizer=summarize).ingest_account({"id": 1}), 1)
        await MailProjectionWorker(store, telegram).run_once()
        self.assertEqual(store.labels[("1", "inbox", "", "1")]["category"], "task")
        self.assertIn("safe summary", raw.text)
        self.assertNotIn("task", raw.text)
        self.assertNotIn("high", raw.text)
        self.assertNotIn("body", raw.text)

    async def test_llm_failure_does_not_drop_or_block_mail(self):
        store = InMemoryMailRepository()

        class Client:
            async def fetch_messages(self, mailbox, *, after_uid=0):
                return [IncomingMail(account_id=1, mailbox=mailbox, uid="2", text_body="body")]

        class Telegram:
            def __init__(self): self.projected = 0
            async def ensure_private_topic(self, account, subject): return 7
            async def project_mail(self, mail, topic): self.projected += 1; return True

        def fail(_: str): raise RuntimeError("model unavailable")
        telegram = Telegram()
        self.assertEqual(await MailIngestionWorker(store, lambda _: Client(), telegram, llm_summarizer=fail).ingest_account({"id": 1}), 1)
        await MailProjectionWorker(store, telegram).run_once()
        self.assertIn(("1", "inbox", "", "2"), store.incoming)
        self.assertEqual(telegram.projected, 1)

    async def test_projection_is_durable_and_retries_after_telegram_failure(self):
        store = InMemoryMailRepository()

        class Client:
            async def fetch_messages(self, mailbox, *, after_uid=0):
                return [IncomingMail(account_id=1, mailbox=mailbox, uid="3", text_body="body")]

        class Telegram:
            def __init__(self): self.fail = True; self.sent = 0
            async def ensure_private_topic(self, account, subject): return 7
            async def project_mail(self, mail, topic):
                if self.fail: raise ConnectionError("temporary telegram failure")
                self.sent += 1
                return True
            async def delete_topic(self, topic): return True

        telegram = Telegram()
        await MailIngestionWorker(store, lambda _: Client(), telegram).ingest_account({"id": 1})
        projection = MailProjectionWorker(store, telegram)
        await projection.run_once()
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "failed")
        telegram.fail = False
        await projection.run_once()
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "delivered")
        self.assertEqual(telegram.sent, 1)

    async def test_projection_marks_provider_mail_read_only_after_telegram_delivery(self):
        store = InMemoryMailRepository()
        events = []
        mail = IncomingMail(account_id=1, mailbox="INBOX", uid="8", uidvalidity="777", subject="new", text_body="body")
        store.enqueue_projection(mail)

        class Telegram:
            async def ensure_private_topic(self, account, subject):
                events.append("topic")
                return 7

            async def project_mail(self, mail, topic):
                events.append("telegram")
                return True

        class IMAP:
            async def mark_read(self, mailbox, uid, *, uidvalidity=None):
                events.append(("read", mailbox, uid, uidvalidity))
                return True

        await MailProjectionWorker(store, Telegram(), lambda _account: IMAP()).run_once()
        self.assertEqual(events, ["topic", "telegram", ("read", "INBOX", "8", "777")])
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "delivered")

    async def test_projection_rejection_does_not_mark_provider_mail_read(self):
        store = InMemoryMailRepository()
        events = []
        store.enqueue_projection(IncomingMail(account_id=1, mailbox="INBOX", uid="9", subject="rejected", text_body="body"))

        class Telegram:
            async def ensure_private_topic(self, account, subject):
                return "topic"

            async def project_mail(self, mail, topic):
                events.append("telegram")
                return False

            async def delete_topic(self, topic):
                events.append("delete")
                return True

        class IMAP:
            async def mark_read(self, *args, **kwargs):
                events.append("read")
                return True

        await MailProjectionWorker(store, Telegram(), lambda _account: IMAP()).run_once()
        self.assertEqual(events, ["telegram", "delete"])
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "failed")

    async def test_provider_read_flag_failure_does_not_replay_telegram(self):
        store = InMemoryMailRepository()
        sent = []
        store.enqueue_projection(IncomingMail(account_id=1, mailbox="INBOX", uid="10", subject="read retry", text_body="body"))

        class Telegram:
            async def ensure_private_topic(self, account, subject):
                return "topic"

            async def project_mail(self, mail, topic):
                sent.append(mail.uid)
                return True

        class IMAP:
            async def mark_read(self, *args, **kwargs):
                return False

        worker = MailProjectionWorker(store, Telegram(), lambda _account: IMAP())
        await worker.run_once()
        await worker.run_once()
        self.assertEqual(sent, ["10"])
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "delivered")

    async def test_legacy_smtp_false_is_ambiguous_not_retried(self):
        store = InMemoryMailRepository()
        operation = MailService(store).queue_send(MailDraft(operation_id="uncertain", account_id=1, from_email="me@example.com", subject="s", to=("to@example.com",)))

        class Client:
            def __init__(self, **kwargs): pass
            async def send_email(self, **kwargs): return False

        worker = MailOutboxWorker(store, lambda _: SMTPTransport({"smtp_server": "smtp", "smtp_port": 465, "smtp_ssl": True, "email": "me@example.com"}, client_cls=Client))
        self.assertEqual((await worker.run_once(operation.id)).state, "ambiguous")
        self.assertIsNone(await worker.run_once(operation.id))

    async def test_explicit_smtp_recipient_rejection_is_failed_not_ambiguous(self):
        store = InMemoryMailRepository()
        operation = MailService(store).queue_send(MailDraft(operation_id="rejected", account_id=1, from_email="me@example.com", subject="s", to=("to@example.com",)))

        class RejectedSMTP:
            async def send(self, message, recipients):
                raise smtplib.SMTPRecipientsRefused({"to@example.com": (550, b"mailbox unavailable")})

        self.assertEqual((await MailOutboxWorker(store, RejectedSMTP()).run_once(operation.id)).state, "failed")
        self.assertIsNone(await MailOutboxWorker(store, RejectedSMTP()).run_once(operation.id))

    async def test_production_smtp_transport_preserves_rejection_and_timeout_classes(self):
        account = {"smtp_server": "smtp", "smtp_port": 465, "smtp_ssl": True, "email": "me@example.com"}
        cases = (
            ("production-rejected", smtplib.SMTPDataError(550, b"message rejected"), "failed"),
            ("production-auth-rejected", smtplib.SMTPAuthenticationError(535, b"credentials rejected"), "failed"),
            ("production-timeout", TimeoutError("timed out after DATA"), "ambiguous"),
        )
        for operation_id, error, expected_state in cases:
            with self.subTest(operation_id=operation_id):
                store = InMemoryMailRepository()
                operation = MailService(store).queue_send(MailDraft(operation_id=operation_id, account_id=1, from_email="me@example.com", subject="s", to=("to@example.com",)))
                transport = SMTPTransport(account)
                with mock.patch.object(transport.client, "_send_via_smtp", side_effect=error):
                    result = await MailOutboxWorker(store, transport).run_once(operation.id)
                self.assertEqual(result.state, expected_state)

    async def test_sqlite_uidvalidity_change_keeps_same_uid_in_each_epoch(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-epoch-worker-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap", "imap_port": 993, "imap_ssl": True,
                                           "smtp_server": "smtp", "smtp_port": 465, "smtp_ssl": True}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            old = IncomingMail(account_id=account["id"], mailbox="INBOX", uid="1", uidvalidity="old", subject="old epoch")
            self.assertTrue(adapter.insert_incoming_if_absent(old))
            adapter.reset_imap_cursor(account["id"], "INBOX", "old", 1)

            class Client:
                def __init__(self): self.after = []
                async def fetch_incremental(self, mailbox, *, after_uid=0):
                    self.after.append(after_uid)
                    if after_uid:
                        return FetchedMessages((), "new")
                    return FetchedMessages((IncomingMail(account_id=account["id"], mailbox=mailbox, uid="1", subject="new epoch"),), "new")

            client = Client()
            self.assertEqual(await MailIngestionWorker(adapter, lambda _: client, object()).ingest_account(account), 1)
            self.assertEqual(client.after, [1, 0])
            conn = repo.db.connect()
            try:
                epochs = [row["uidvalidity"] for row in conn.execute("SELECT uidvalidity FROM emails WHERE account_id = ? AND mailbox = ? AND uid = ? ORDER BY uidvalidity", (account["id"], "INBOX", "1"))]
            finally:
                conn.close()
            self.assertEqual(epochs, ["new", "old"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try: os.unlink(path + suffix)
                except FileNotFoundError: pass

    async def test_delete_never_uses_mailbox_wide_expunge_without_uidplus(self):
        class Conn:
            capabilities = ()
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", []
            def uid(self, command, *args): self.calls.append((command, *args)); return "OK", []

        class Client:
            def __init__(self, account): self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=Client)
        self.assertFalse(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
        self.assertNotIn(("EXPUNGE", "5"), transport.client.conn.calls)
        self.assertIn(("STORE", "5", "-FLAGS.SILENT", r"(\Deleted)"), transport.client.conn.calls)

    async def test_delete_uses_uid_expunge_when_uidplus_available(self):
        class Conn:
            capabilities = (b"IMAP4REV1", b"UIDPLUS")
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", []
            def uid(self, command, *args): self.calls.append((command, *args)); return "OK", []

        class Client:
            def __init__(self, account): self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=Client)
        self.assertTrue(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
        self.assertIn(("EXPUNGE", "5"), transport.client.conn.calls)

    async def test_delete_accepts_exact_deleted_flag_without_uidplus(self):
        class Conn:
            capabilities = ()
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", []
            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "SEARCH":
                    return "OK", [b"5"]
                return "OK", []

        class Client:
            def __init__(self, account): self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=Client)
        self.assertTrue(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
        self.assertIn(("STORE", "5", r"+FLAGS.SILENT", r"(\Deleted)"), transport.client.conn.calls)
        self.assertNotIn(("EXPUNGE", "5"), transport.client.conn.calls)
        self.assertNotIn(("STORE", "5", "-FLAGS.SILENT", r"(\Deleted)"), transport.client.conn.calls)

    async def test_delete_retry_succeeds_when_uid_is_already_absent(self):
        class Conn:
            capabilities = ()
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", []
            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "SEARCH":
                    return "OK", [b""]
                return "OK", []

        class Client:
            def __init__(self, account): self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=Client)
        self.assertTrue(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
        self.assertEqual(
            transport.client.conn.calls,
            [("SEARCH", None, "UID", "5")],
        )

    async def test_gmail_delete_applies_trash_label_to_exact_uid(self):
        class Conn:
            capabilities = (b"IMAP4REV1", b"X-GM-EXT-1")
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", []
            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "SEARCH":
                    return "OK", [b"5"]
                return "OK", []

        class Client:
            def __init__(self, account): self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@gmail.com"}, client_cls=Client)
        self.assertTrue(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
        self.assertIn(
            ("STORE", "5", "+X-GM-LABELS", r"(\Trash)"),
            transport.client.conn.calls,
        )
        self.assertNotIn(
            ("STORE", "5", "+FLAGS.SILENT", r"(\Deleted)"),
            transport.client.conn.calls,
        )

    async def test_move_capable_provider_moves_uid_to_special_use_trash(self):
        class Conn:
            capabilities = (b"IMAP4REV1", b"MOVE")
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", []
            def list(self, reference, pattern):
                self.calls.append(("LIST", reference, pattern))
                return "OK", [b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"']
            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "SEARCH":
                    return "OK", [b"5"]
                if command == "MOVE":
                    return "OK", []
                raise AssertionError(command)

        class Client:
            def __init__(self, account): self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@icloud.com"}, client_cls=Client)
        self.assertTrue(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
        self.assertIn(("MOVE", "5", "Deleted Messages"), transport.client.conn.calls)
        self.assertNotIn(("EXPUNGE", "5"), transport.client.conn.calls)

    async def test_icloud_falls_back_to_quoted_deleted_messages_mailbox(self):
        class Conn:
            capabilities = ()
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", []
            def list(self, reference, pattern): raise RuntimeError("server rejected unquoted LIST")
            def _simple_command(self, command, *args):
                self.calls.append((command, *args))
                return "OK", [b'(\\HasNoChildren) "/" "Deleted Messages"']
            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "SEARCH": return "OK", [b"5"]
                if command == "MOVE": return "OK", []
                raise AssertionError(command)

        class Client:
            def __init__(self, account):
                self.account_info = account
                self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@icloud.com", "imap_server": "imap.mail.me.com"}, client_cls=Client)
        self.assertTrue(await transport.delete({"mailbox": "INBOX", "uid": "5"}))
        self.assertIn(("MOVE", "5", '"Deleted Messages"'), transport.client.conn.calls)

    async def test_delete_refuses_reused_uid_after_uidvalidity_changes(self):
        class Conn:
            capabilities = (b"IMAP4REV1", b"UIDPLUS")
            def __init__(self): self.calls = []
            def select(self, mailbox): return "OK", [b"[UIDVALIDITY 222]"]
            def uid(self, command, *args): self.calls.append((command, *args)); return "OK", []

        class Client:
            def __init__(self, account): self.conn = Conn()
            def connect(self): return True
            def disconnect(self): pass

        transport = IMAPTransport({"id": 1, "email": "me@example.com"}, client_cls=Client)
        with self.assertRaisesRegex(RuntimeError, "UIDVALIDITY changed"):
            await transport.delete({"mailbox": "INBOX", "uid": "5", "uidvalidity": "111"})
        self.assertEqual(transport.client.conn.calls, [])


class TestV2UidCursor(unittest.TestCase):
    def test_sqlite_adapter_reads_max_uid_per_mailbox(self):
        from app.db import V2Repository
        import tempfile
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-cursor-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap", "imap_port": 993, "imap_ssl": True,
                                           "smtp_server": "smtp", "smtp_port": 465, "smtp_ssl": True}, "secret")
            repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="9")
            repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="15")
            repo.insert_incoming_if_absent(account["id"], mailbox="Archive", uid="99")
            self.assertEqual(V2MailRepositoryAdapter(repo).max_ingested_uid(account["id"], "INBOX"), 15)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try: os.unlink(path + suffix)
                except FileNotFoundError: pass

    def test_sqlite_cursor_requires_reset_when_uidvalidity_changes(self):
        from app.db import V2Repository
        import tempfile
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-uidvalidity-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap", "imap_port": 993, "imap_ssl": True,
                                           "smtp_server": "smtp", "smtp_port": 465, "smtp_ssl": True}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            self.assertEqual(adapter.get_imap_cursor(account["id"], "INBOX")["last_uid"], 0)
            adapter.advance_imap_cursor(account["id"], "INBOX", "one", 9)
            self.assertTrue(adapter.advance_imap_cursor(account["id"], "INBOX", "two", 1)["reset_required"])
            self.assertEqual(adapter.reset_imap_cursor(account["id"], "INBOX", "two", 0)["last_uid"], 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try: os.unlink(path + suffix)
                except FileNotFoundError: pass

    def test_runtime_uses_polling_interval_configuration_by_default(self):
        with mock.patch.dict(os.environ, {"POLLING_INTERVAL": "37"}, clear=False):
            self.assertEqual(MailWorkerRuntime().poll_seconds, 37.0)
