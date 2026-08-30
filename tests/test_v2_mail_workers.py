import asyncio
import os
import tempfile
import unittest
from unittest import mock

from app.integrations.mail import Attachment, DeleteOperation, IncomingMail, MailComposer, MailDraft, MailTelegramProjection, RecipientRequired, TelegramTopic
from app.integrations.telegram_http import MiniAppOnlyProjection, TelegramApiError
from app.services import InMemoryMailRepository, MailService, V2MailRepositoryAdapter
from app.workers import MailDeleteWorker, MailIngestionWorker, MailOutboxWorker, MailProjectionWorker


def draft(operation_id="send-1", **overrides):
    values = dict(operation_id=operation_id, account_id=1, from_email="me@example.com", subject="subject", text_body="body", to=("to@example.com",))
    values.update(overrides)
    return MailDraft(**values)


class _SMTP:
    def __init__(self, error=None):
        self.calls, self.error = 0, error

    async def send(self, message, recipients):
        self.calls += 1
        if self.error:
            raise self.error
        return True


class TestV2Outbox(unittest.IsolatedAsyncioTestCase):
    async def test_double_click_claims_once_and_has_stable_message_id(self):
        store, smtp = InMemoryMailRepository(), _SMTP()
        op = MailService(store).queue_send(draft())
        worker = MailOutboxWorker(store, smtp, worker_id="one")
        first, second = await asyncio.gather(worker.run_once(op.id), worker.run_once(op.id))
        self.assertEqual(smtp.calls, 1)
        self.assertEqual(store.get_send(op.id).state, "sent")
        self.assertEqual(store.account_statuses["1"], {"connection_status": "connected", "connection_error": None})
        self.assertEqual(op.draft.message_id, MailComposer.compose(op.draft)[1])
        self.assertEqual(sum(x is not None for x in (first, second)), 1)

    async def test_expired_crash_lease_is_recovered(self):
        store, smtp = InMemoryMailRepository(), _SMTP()
        op = MailService(store).queue_send(draft())
        stale = store.claim_send(op.id, "crashed", 1, now=0)
        self.assertEqual(stale.state, "sending")
        result = await MailOutboxWorker(store, smtp).run_once(op.id)
        # The real clock is after the artificial expired lease.
        self.assertEqual(result.state, "sent")
        self.assertEqual(smtp.calls, 1)

    async def test_timeout_is_ambiguous_and_is_not_resent(self):
        store, smtp = InMemoryMailRepository(), _SMTP(TimeoutError("socket timeout"))
        op = MailService(store).queue_send(draft())
        worker = MailOutboxWorker(store, smtp)
        self.assertEqual((await worker.run_once(op.id)).state, "ambiguous")
        self.assertIsNone(await worker.run_once(op.id))
        self.assertEqual(smtp.calls, 1)
        self.assertEqual(store.account_statuses["1"]["connection_status"], "failed")


class TestV2Composition(unittest.TestCase):
    def test_reply_all_deduplicates_and_excludes_self(self):
        result = MailComposer.reply_all(draft(to=()), parent_from="Sender <sender@example.com>", parent_to=["me@example.com", "Other <OTHER@example.com>"], parent_cc=["other@example.com", "cc@example.com"], parent_message_id="<parent@example.com>")
        self.assertEqual(result.to, ("sender@example.com",))
        self.assertEqual(result.cc, ("OTHER@example.com", "cc@example.com"))
        self.assertEqual(result.in_reply_to, "<parent@example.com>")
        self.assertEqual(result.references, ("<parent@example.com>",))

    def test_empty_recipient_rejected_before_enqueue(self):
        with self.assertRaises(RecipientRequired):
            MailService(InMemoryMailRepository()).queue_send(draft(to=(), cc=(), bcc=()))

    def test_markdown_html_and_attachment_are_composed(self):
        markdown = (
            "## Heading\n\n**bold** *italic* ~~strike~~\n\n"
            "- one\n- two\n\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "[guide](https://example.test)\n\n<script>alert(1)</script>"
        )
        msg, message_id, _ = MailComposer.compose(draft(markdown_body=markdown, attachments=(Attachment("a.txt", b"hello", "text/plain"),)))
        self.assertEqual(msg["Message-ID"], message_id)
        html_part = next(part for part in msg.walk() if part.get_content_type() == "text/html")
        html = html_part.get_payload(decode=True).decode("utf-8")
        self.assertIn("<h2>Heading</h2>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<s>strike</s>", html)
        self.assertIn("<table>", html)
        self.assertIn('href="https://example.test"', html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertTrue(any(part.get_filename() == "a.txt" for part in msg.walk()))


class _IMAP:
    def __init__(self, messages): self.messages = messages
    async def fetch_messages(self, mailbox): return self.messages


class _FailingIMAP:
    async def fetch_messages(self, mailbox):
        raise ConnectionError("imap unavailable")


class _Telegram:
    def __init__(self): self.created = 0; self.projected = []; self.deletes = 0; self.fail_delete = False
    async def ensure_private_topic(self, account, subject): self.created += 1; return 9
    async def project_mail(self, mail, topic): self.projected.append((mail.uid, topic))
    async def delete_topic(self, topic):
        self.deletes += 1
        return not self.fail_delete


class TestV2IngestionAndDelete(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_refreshes_the_inbox_after_topic_activity(self):
        refreshed = []

        class RawTelegram:
            async def send_message(self, _chat, _text, **_kwargs):
                return [{"message_id": 1}]

        projection = MailTelegramProjection(
            RawTelegram(), 42, after_delivery=lambda: refreshed.append(True)
        )

        await projection.project_mail(
            IncomingMail(account_id=1, mailbox="INBOX", uid="1", subject="hello", text_body="body"),
            TelegramTopic(42, 55),
        )

        self.assertEqual(refreshed, [True])

    async def test_html_mail_adds_safe_topic_links_and_preserves_source_as_primary_document(self):
        class RawTelegram:
            def __init__(self):
                self.messages = []
                self.documents = []

            async def send_message(self, chat, text, **kwargs):
                self.messages.append((chat, text, kwargs))
                return [{"message_id": len(self.messages)}]

            async def send_document(self, chat, document, **kwargs):
                self.documents.append((chat, document, kwargs))
                return {"message_id": 99}

        raw = RawTelegram()
        projection = MailTelegramProjection(raw, 42)
        html = (
            "<p>Read <a href='https://example.test/read'>the guide</a>.</p>"
            "<a href='https://example.test/unsubscribe'>Unsubscribe</a>"
        )

        await projection.project_mail(
            IncomingMail(
                account_id=1,
                mailbox="INBOX",
                uid="html-1",
                subject="Quarterly report",
                text_body="Read the guide.",
                html_body=html,
                important_links=(
                    {"caption": "the guide", "link": "https://example.test/read"},
                    {"caption": "退订", "link": "https://example.test/unsubscribe"},
                ),
            ),
            TelegramTopic(42, 55, 123),
        )

        markup = raw.documents[0][2]["reply_markup"]["inline_keyboard"]
        self.assertEqual(markup[1], [{"text": "the guide", "url": "https://example.test/read"}])
        self.assertEqual(markup[2], [{"text": "退订", "url": "https://example.test/unsubscribe"}])
        self.assertEqual(raw.messages, [])
        self.assertNotIn("example.test/read", raw.documents[0][2]["caption"])
        self.assertEqual(raw.documents[0][0], 42)
        self.assertEqual(raw.documents[0][1], html.encode("utf-8"))
        self.assertEqual(raw.documents[0][2]["filename"], "Quarterly report.html")
        self.assertEqual(raw.documents[0][2]["message_thread_id"], 55)
        self.assertEqual(raw.documents[0][2]["content_type"], "text/html")

    async def test_html_mail_uses_one_primary_document_with_card_caption_and_sends_only_real_attachments(self):
        class RawTelegram:
            def __init__(self):
                self.messages = []
                self.documents = []

            async def send_message(self, chat, text, **kwargs):
                self.messages.append((chat, text, kwargs))
                return [{"message_id": 10}]

            async def send_document(self, chat, document, **kwargs):
                self.documents.append((chat, document, kwargs))
                return {"message_id": 20 + len(self.documents)}

        raw = RawTelegram()
        projection = MailTelegramProjection(raw, 42)
        await projection.project_mail(
            IncomingMail(
                account_id=1, mailbox="INBOX", uid="html-primary", subject="Report",
                sender="sender@example.com", text_body="summary", html_body="<p>source</p>",
                attachments=(
                    Attachment("invoice.pdf", b"pdf", "application/pdf"),
                    Attachment("logo.png", b"png", "image/png", content_id="cid:logo", is_inline=True),
                ),
                important_links=({"caption": "Open", "link": "https://example.test"},),
            ),
            TelegramTopic(42, 55, 123),
        )
        self.assertEqual(raw.messages, [])
        self.assertEqual(len(raw.documents), 2)
        self.assertEqual(raw.documents[0][1], b"<p>source</p>")
        self.assertLessEqual(len(raw.documents[0][2]["caption"]), 1024)
        self.assertEqual(raw.documents[0][2]["disable_notification"], False)
        self.assertIn("主题", raw.documents[0][2]["caption"])
        self.assertIn("发件人", raw.documents[0][2]["caption"])
        self.assertIn("摘要", raw.documents[0][2]["caption"])
        self.assertEqual(raw.documents[1][1], b"pdf")
        self.assertTrue(raw.documents[1][2]["disable_notification"])

    async def test_oversized_html_falls_back_to_visible_text_card(self):
        class RawTelegram:
            def __init__(self):
                self.messages = []
                self.documents = []

            async def send_document(self, chat, document, **kwargs):
                self.documents.append((chat, document, kwargs))
                return MiniAppOnlyProjection(len(document), kwargs.get("filename"))

            async def send_message(self, chat, text, **kwargs):
                self.messages.append((chat, text, kwargs))
                return [{"message_id": 101}]

        raw = RawTelegram()
        result = await MailTelegramProjection(raw, 42).project_mail(
            IncomingMail(account_id=1, mailbox="INBOX", uid="oversize", subject="Big", text_body="fallback", html_body="<p>too big</p>"),
            TelegramTopic(42, 55, 123),
        )
        self.assertEqual(len(raw.documents), 1)
        self.assertEqual(len(raw.messages), 1)
        self.assertEqual(result[0]["message_id"], 101)
        self.assertEqual(result[0]["message_kind"], "text")

    async def test_plain_mail_uses_one_text_card(self):
        class RawTelegram:
            def __init__(self):
                self.messages = []
                self.documents = []

            async def send_message(self, chat, text, **kwargs):
                self.messages.append((chat, text, kwargs))
                return [{"message_id": 10}]

            async def send_document(self, *args, **kwargs):
                self.documents.append((args, kwargs))
                return {"message_id": 20}

        raw = RawTelegram()
        await MailTelegramProjection(raw, 42).project_mail(
            IncomingMail(account_id=1, mailbox="INBOX", uid="text", subject="Subject", sender="s", text_body="body"),
            TelegramTopic(42, 55, 123),
        )
        self.assertEqual(len(raw.messages), 1)
        self.assertEqual(raw.documents, [])

    async def test_html_primary_result_marks_message_kind_for_durable_summary_refresh(self):
        class RawTelegram:
            async def send_document(self, chat, document, **kwargs):
                return {"message_id": 20}

        result = await MailTelegramProjection(RawTelegram(), 42).project_mail(
            IncomingMail(
                account_id=1, mailbox="INBOX", uid="html-kind", subject="Subject",
                sender="sender@example.com", text_body="body", html_body="<p>source</p>",
            ),
            TelegramTopic(42, 55, 123),
        )
        self.assertEqual(result[0]["message_kind"], "html")

    async def test_summary_updates_edit_caption_for_html_and_text_for_plain(self):
        class RawTelegram:
            def __init__(self):
                self.edits = []

            async def edit_message_caption(self, chat, message_id, caption, **kwargs):
                self.edits.append(("caption", chat, message_id, caption, kwargs))

            async def edit_message_text(self, chat, message_id, text, **kwargs):
                self.edits.append(("text", chat, message_id, text, kwargs))

        class Repo:
            def __init__(self):
                self.parts = [{"telegram_message_id": 10, "telegram_chat_id": 42, "part_index": 0, "kind": "html"}]

            def get_email(self, _email_id):
                return {"id": 1, "thread_id": 123, "llm_important_links_json": "[]"}

            def list_telegram_delivery_parts(self, _email_id):
                return self.parts

        raw, repo = RawTelegram(), Repo()
        projection = MailTelegramProjection(raw, 42, repository=repo)
        await projection.update_mail_summary(IncomingMail(
            account_id=1, mailbox="INBOX", uid="x", email_id=1, subject="s", sender="from",
            text_body="body", html_body="<p>html</p>", summary="updated",
        ))
        self.assertEqual(raw.edits[0][0], "caption")

        repo.parts = [{"telegram_message_id": 11, "telegram_chat_id": 42, "part_index": 0, "kind": "text"}]
        raw.edits.clear()
        await projection.update_mail_summary(IncomingMail(
            account_id=1, mailbox="INBOX", uid="x", email_id=1, subject="s", sender="from",
            text_body="body", html_body=None, summary="updated",
        ))
        self.assertEqual(raw.edits[0][0], "text")

    async def test_missing_topic_mapping_is_not_reported_as_a_delete_success(self):
        class RawTelegram:
            async def delete_forum_topic(self, _chat, _thread):
                raise TelegramApiError("deleteForumTopic", "Bad Request: message thread not found")

        projection = MailTelegramProjection(RawTelegram(), 42)
        with self.assertRaises(TelegramApiError):
            await projection.delete_topic(TelegramTopic(42, 55))

    async def test_delete_topic_requires_explicit_true_confirmation(self):
        class RawTelegram:
            async def delete_forum_topic(self, _chat, _thread):
                return None

        projection = MailTelegramProjection(RawTelegram(), 42)
        self.assertFalse(await projection.delete_topic(TelegramTopic(42, 55)))

    async def test_uid_idempotence_and_single_instance_lease(self):
        store, tg = InMemoryMailRepository(), _Telegram()
        mail = IncomingMail(account_id=1, mailbox="INBOX", uid="7", message_id="<m>", sender="sender@example.com", to=("me@example.com",), subject="hello", text_body="body")
        worker = MailIngestionWorker(store, lambda _: _IMAP([mail]), tg, worker_id="a")
        self.assertEqual(await worker.ingest_account({"id": 1}), 1)
        self.assertEqual(await worker.ingest_account({"id": 1}), 0)
        await MailProjectionWorker(store, tg).run_once()
        self.assertEqual(tg.created, 1)
        self.assertIn(("1", "sender@example.com"), store.contacts)
        self.assertEqual(store.account_statuses["1"], {"connection_status": "connected", "connection_error": None})

    async def test_sync_failure_updates_account_status_without_swallowing_error(self):
        store = InMemoryMailRepository()
        worker = MailIngestionWorker(store, lambda _: _FailingIMAP(), _Telegram(), worker_id="failed-sync")
        with self.assertRaises(ConnectionError):
            await worker.ingest_account({"id": 1})
        self.assertEqual(store.account_statuses["1"]["connection_status"], "failed")
        self.assertEqual(store.account_statuses["1"]["connection_error"], "connection")

    async def test_unbound_private_chat_persists_mail_and_waits_for_projection(self):
        store = InMemoryMailRepository()
        mail = IncomingMail(account_id=1, mailbox="INBOX", uid="waiting", sender="sender@example.com", to=("me@example.com",), subject="hello")

        class RawTelegram:
            async def create_forum_topic(self, chat, subject):
                raise AssertionError("must not create a topic before binding")

        worker = MailIngestionWorker(store, lambda _: _IMAP([mail]), MailTelegramProjection(RawTelegram(), lambda _: None))
        self.assertEqual(await worker.ingest_account({"id": 1}), 1)
        self.assertIn(("1", "inbox", "", "waiting"), store.projection_waiting)

    async def test_empty_mail_is_marked_delivered_without_creating_a_topic(self):
        store, tg = InMemoryMailRepository(), _Telegram()
        mail = IncomingMail(account_id=1, mailbox="INBOX", uid="empty", subject="headers only")
        store.enqueue_projection(mail)

        await MailProjectionWorker(store, tg).run_once()

        self.assertEqual(tg.created, 0)
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "delivered")

    async def test_first_send_failure_deletes_only_the_new_empty_topic_and_retry_is_idempotent(self):
        store = InMemoryMailRepository()
        mail = IncomingMail(account_id=1, mailbox="INBOX", uid="retry", subject="retry", text_body="body")
        store.enqueue_projection(mail)

        class Telegram:
            def __init__(self):
                self.created, self.deleted, self.sent, self.fail = [], [], 0, True
            async def ensure_private_topic(self, _account, _subject):
                topic = f"topic-{len(self.created) + 1}"
                self.created.append(topic)
                return topic
            async def project_mail(self, _mail, _topic):
                if self.fail:
                    raise ConnectionError("Telegram unavailable before send")
                self.sent += 1
                return [{"message_id": 1}]
            async def delete_topic(self, topic):
                self.deleted.append(topic)
                return True

        telegram = Telegram()
        worker = MailProjectionWorker(store, telegram)
        await worker.run_once()
        self.assertEqual(telegram.created, ["topic-1"])
        self.assertEqual(telegram.deleted, ["topic-1"])
        self.assertNotIn(("1", "inbox", "", "retry"), store.email_threads)
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "failed")

        telegram.fail = False
        await worker.run_once()
        self.assertEqual(telegram.created, ["topic-1", "topic-2"])
        self.assertEqual(telegram.deleted, ["topic-1"])
        self.assertEqual(telegram.sent, 1)
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "delivered")
        self.assertIsNone(await worker.run_once())

    async def test_ambiguous_telegram_send_keeps_new_topic_and_requires_reconciliation(self):
        store = InMemoryMailRepository()
        mail = IncomingMail(account_id=1, mailbox="INBOX", uid="ambiguous", subject="keep", text_body="body")
        store.enqueue_projection(mail)

        class RawTelegram:
            def __init__(self):
                self.created = self.deleted = self.send_attempts = 0
            async def create_forum_topic(self, _chat, _subject):
                self.created += 1
                return {"message_thread_id": 55}
            async def send_message(self, _chat, _text, **_kwargs):
                self.send_attempts += 1
                raise TelegramApiError("sendMessage", "request timed out", ambiguous=True)
            async def delete_forum_topic(self, _chat, _thread_id):
                self.deleted += 1
                return True

        raw = RawTelegram()
        projection = MailTelegramProjection(raw, 42)
        worker = MailProjectionWorker(store, projection)
        await worker.run_once()

        self.assertEqual(raw.created, 1)
        self.assertEqual(raw.send_attempts, 1)
        self.assertEqual(raw.deleted, 0)
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "ambiguous")
        self.assertIsNone(await worker.run_once())

    async def test_failed_empty_topic_cleanup_is_ambiguous_and_never_creates_another_topic(self):
        store = InMemoryMailRepository()
        mail = IncomingMail(account_id=1, mailbox="INBOX", uid="cleanup", subject="cleanup", text_body="body")
        store.enqueue_projection(mail)

        class Telegram:
            def __init__(self):
                self.created = self.deleted = 0
            async def ensure_private_topic(self, _account, _subject):
                self.created += 1
                return "topic-1"
            async def project_mail(self, _mail, _topic):
                raise ConnectionError("definitely not sent")
            async def delete_topic(self, _topic):
                self.deleted += 1
                return False

        telegram = Telegram()
        worker = MailProjectionWorker(store, telegram)
        await worker.run_once()

        self.assertEqual(telegram.created, 1)
        self.assertEqual(telegram.deleted, 1)
        self.assertEqual(next(iter(store.projection_jobs.values()))["status"], "ambiguous")
        self.assertIsNone(await worker.run_once())
        self.assertEqual(telegram.created, 1)

    async def test_delete_saga_resumes_after_telegram_failure(self):
        store, tg = InMemoryMailRepository(), _Telegram()
        op = DeleteOperation("del-1", 1, "INBOX", "3", provider_mapping={"uid": "3"}, telegram_topic_id=4)
        store.enqueue_delete(op)

        class Provider:
            def __init__(self): self.calls = 0
            async def delete(self, mapping): self.calls += 1; return True
        provider = Provider()
        tg.fail_delete = True
        worker = MailDeleteWorker(store, provider, tg)
        self.assertEqual((await worker.run_once()).state, "queued")
        self.assertEqual(provider.calls, 0)
        tg.fail_delete = False
        self.assertEqual((await worker.run_once()).state, "tombstoned")
        self.assertEqual(tg.deletes, 2)
        self.assertEqual(provider.calls, 1)
        self.assertIn(("1", "inbox", "", "3"), store.tombstones)

    async def test_delete_saga_skips_topic_after_optimistic_ui_removal(self):
        store, tg = InMemoryMailRepository(), _Telegram()
        op = DeleteOperation(
            "del-optimistic", 1, "INBOX", "3", provider_mapping={"uid": "3"},
            telegram_topic_id=4, topic_deleted=True,
        )
        store.enqueue_delete(op)

        class Provider:
            async def delete(self, _mapping):
                return True

        result = await MailDeleteWorker(store, Provider(), tg).run_once()

        self.assertEqual(result.state, "tombstoned")
        self.assertEqual(tg.deletes, 0)

    async def test_delete_saga_does_not_tombstone_when_topic_mapping_is_invalid(self):
        store = InMemoryMailRepository()
        op = DeleteOperation(
            "del-invalid-topic", 1, "INBOX", "3", provider_mapping={"uid": "3"},
            telegram_topic_id=TelegramTopic(42, 99),
        )
        store.enqueue_delete(op)

        class Provider:
            async def delete(self, _mapping):
                return True

        class RawTelegram:
            async def delete_forum_topic(self, _chat, _thread):
                raise TelegramApiError("deleteForumTopic", "Bad Request: TOPIC_ID_INVALID")

        projection = MailTelegramProjection(RawTelegram(), 42)
        result = await MailDeleteWorker(store, Provider(), projection).run_once()

        self.assertEqual(result.state, "queued")
        persisted = store.deletes["del-invalid-topic"]
        self.assertEqual(persisted.state, "queued")
        self.assertIsNotNone(persisted.error)
        self.assertNotIn(("1", "inbox", "", "3"), store.tombstones)

    async def test_delete_saga_adapts_raw_forum_topic_client(self):
        store = InMemoryMailRepository()

        class RawTelegram:
            def __init__(self):
                self.deleted = []

            async def delete_forum_topic(self, chat_id, message_thread_id):
                self.deleted.append((chat_id, message_thread_id))
                return True

        telegram = RawTelegram()
        op = DeleteOperation(
            "del-raw", 1, "INBOX", "3", provider_mapping={"uid": "3"},
            telegram_topic_id=TelegramTopic(42, 99),
        )
        store.enqueue_delete(op)

        class Provider:
            async def delete(self, _mapping):
                return True

        result = await MailDeleteWorker(store, Provider(), telegram).run_once()

        self.assertEqual(result.state, "tombstoned")
        self.assertEqual(telegram.deleted, [(42, 99)])

    async def test_delete_saga_requires_true_from_raw_forum_topic_client(self):
        store = InMemoryMailRepository()

        class RawTelegram:
            async def delete_forum_topic(self, _chat_id, _message_thread_id):
                return None

        telegram = RawTelegram()
        op = DeleteOperation(
            "del-raw-unconfirmed", 1, "INBOX", "3", provider_mapping={"uid": "3"},
            telegram_topic_id=TelegramTopic(42, 99),
        )
        store.enqueue_delete(op)

        class Provider:
            async def delete(self, _mapping):
                return True

        result = await MailDeleteWorker(store, Provider(), telegram).run_once()

        self.assertEqual(result.state, "queued")
        self.assertIn("not confirmed", result.error or "")
        self.assertNotIn(("1", "inbox", "", "3"), store.tombstones)

    async def test_delete_saga_refreshes_inbox_after_tombstone(self):
        store, telegram = InMemoryMailRepository(), _Telegram()
        op = DeleteOperation(
            "del-refresh", 1, "INBOX", "3", provider_mapping={"uid": "3"},
            telegram_topic_id=4,
        )
        store.enqueue_delete(op)
        refreshed = []

        class Provider:
            async def delete(self, _mapping):
                return True

        async def refresh():
            refreshed.append(True)

        result = await MailDeleteWorker(
            store, Provider(), telegram, after_delete=refresh
        ).run_once()

        self.assertEqual(result.state, "tombstoned")
        self.assertEqual(refreshed, [True])

    async def test_missing_provider_mapping_is_not_success(self):
        store, tg = InMemoryMailRepository(), _Telegram()
        op = DeleteOperation("del-2", 1, "INBOX", "3")
        store.enqueue_delete(op)
        result = await MailDeleteWorker(store, object(), tg).run_once()
        self.assertEqual(result.state, "failed")
        self.assertNotIn(("1", "inbox", "", "3"), store.tombstones)

    async def test_delete_failure_does_not_send_a_proactive_notification(self):
        store = InMemoryMailRepository()
        op = DeleteOperation(
            "del-no-notify", 1, "INBOX", "3",
            provider_mapping={"uid": "3"}, telegram_topic_id=4,
        )
        store.enqueue_delete(op)

        class Telegram(_Telegram):
            async def send_message(self, *_args, **_kwargs):
                raise AssertionError("delete failures must not notify the user")

        class Provider:
            async def delete(self, _mapping):
                return False

        result = await MailDeleteWorker(store, Provider(), Telegram()).run_once()

        self.assertEqual("telegram_deleted", result.state)
        self.assertFalse(result.tombstoned)


class TestV2SQLiteAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_real_attachments_are_persisted_rehydrated_and_cleaned_with_thread(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-incoming-attachment-", suffix=".db")
        os.close(fd)
        with tempfile.TemporaryDirectory() as data_dir, mock.patch.dict(
            os.environ, {"TELEGRAMAIL_DATA_DIR": data_dir}
        ):
            try:
                repo = V2Repository(path, master_key=b"x" * 32)
                repo.bind_admin(42, private_chat_id=9001)
                account = repo.create_account({
                    "email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                    "smtp_server": "smtp.example.com", "smtp_port": 465,
                }, "secret")
                adapter = V2MailRepositoryAdapter(repo)
                mail = IncomingMail(
                    account_id=account["id"], mailbox="INBOX", uid="attachment-1",
                    message_id="<attachment@test>", sender="sender@example.com", subject="Files",
                    text_body="See attachment",
                    attachments=(
                        Attachment("invoice.pdf", b"pdf-bytes", "application/pdf"),
                        Attachment("logo.png", b"image", "image/png", content_id="logo", is_inline=True),
                    ),
                )
                self.assertTrue(adapter.insert_incoming_if_absent(mail))
                email_row = repo.get_email_by_imap_uid(account["id"], mailbox="INBOX", uid="attachment-1")
                persisted = repo.list_email_attachments(email_row["id"])
                self.assertEqual(len(persisted), 1)
                local_path = os.path.join(data_dir, persisted[0]["local_path"])
                self.assertTrue(os.path.isfile(local_path))

                adapter.enqueue_projection(mail)
                job = adapter.claim_next_projection("worker", 60)
                self.assertEqual([item.filename for item in job.mail.attachments], ["invoice.pdf"])

                thread = repo.assign_thread(
                    email_row["id"], telegram_chat_id=9001,
                    telegram_message_thread_id=77, subject="Files",
                )
                operation = repo.enqueue_delete(account["id"], "delete-attachment", thread_id=thread["id"])
                repo.update_delete(operation["id"], status="deleting", provider_deleted=True, topic_deleted=True)
                self.assertIsNotNone(repo.tombstone_thread(thread["id"], delete_operation_id=operation["id"]))
                self.assertFalse(os.path.exists(local_path))
                self.assertEqual(repo.list_email_attachments(email_row["id"]), [])
            finally:
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(path + suffix)
                    except FileNotFoundError:
                        pass
    async def test_smtp_success_after_lease_expiry_is_quarantined_without_resend(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-send-lease-race-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "race@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "smtp_server": "smtp.example.com", "smtp_port": 465}, "secret")
            draft_row = repo.create_draft(account["id"], from_identity_email="race@example.com", subject="race", body_markdown="body")
            repo.replace_draft_recipients(draft_row["id"], [{"type": "to", "email": "to@example.com"}])
            operation = repo.create_send_operation(account["id"], "lease-race", draft_id=draft_row["id"])

            class SMTP:
                def __init__(self):
                    self.calls = 0

                async def send(self, _message, _recipients):
                    self.calls += 1
                    conn = repo.db.connect()
                    try:
                        conn.execute("UPDATE send_operations SET lease_until = 0 WHERE id = ?", (operation["id"],))
                        conn.commit()
                    finally:
                        conn.close()
                    return True

            smtp = SMTP()
            adapter = V2MailRepositoryAdapter(repo)
            worker = MailOutboxWorker(adapter, smtp, worker_id="race-worker", lease_seconds=60)
            result = await worker.run_once(str(operation["id"]))
            self.assertEqual(result.state, "ambiguous")
            self.assertEqual(smtp.calls, 1)
            self.assertEqual(repo.get_send(operation["id"])["status"], "ambiguous")
            self.assertIsNone(await worker.run_once(str(operation["id"])))
            self.assertEqual(smtp.calls, 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_real_v2_repository_claims_and_sends_outside_its_transaction(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-mail-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42, private_chat_id=-1001)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True}, "secret")
            saved = repo.create_draft(account["id"], from_identity_email="me@example.com", subject="integrated", body_markdown="body")
            repo.replace_draft_recipients(saved["id"], [{"type": "to", "email": "to@example.com"}])
            raw = repo.create_send_operation(account["id"], "integration-op", draft_id=saved["id"])
            smtp = _SMTP()
            result = await MailOutboxWorker(V2MailRepositoryAdapter(repo), smtp, worker_id="sqlite").run_once(str(raw["id"]))
            self.assertEqual(result.state, "sent")
            self.assertEqual(repo.get_send(raw["id"])["status"], "sent")
            self.assertEqual(repo.get_account(account["id"])["connection_status"], "connected")
            self.assertEqual(smtp.calls, 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_real_v2_ambiguous_send_can_be_reconciled_without_resend(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-reconcile-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True}, "secret")
            saved = repo.create_draft(account["id"], from_identity_email="me@example.com", subject="reconcile", body_markdown="body")
            repo.replace_draft_recipients(saved["id"], [{"type": "to", "email": "to@example.com"}])
            raw = repo.create_send_operation(account["id"], "reconcile-op", draft_id=saved["id"])
            adapter = V2MailRepositoryAdapter(repo)
            smtp = _SMTP(TimeoutError("post-DATA timeout"))

            class Reconciler:
                async def has_message_id(self, message_id, operation):
                    return True

            worker = MailOutboxWorker(adapter, smtp, reconciler=Reconciler(), worker_id="sqlite")
            self.assertEqual((await worker.run_once(str(raw["id"]))).state, "ambiguous")
            self.assertTrue(await worker.reconcile_ambiguous(str(raw["id"])))
            self.assertEqual(repo.get_send(raw["id"])["status"], "sent")
            self.assertEqual(smtp.calls, 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_real_v2_ingestion_and_delete_saga_mapping(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-saga-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42, private_chat_id=-1001)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            mail = IncomingMail(account_id=account["id"], mailbox="INBOX", uid="3", message_id="<incoming@test>", sender="sender@example.com", to=("me@example.com",), subject="hello", text_body="body")

            class RawTelegram:
                def __init__(self): self.deleted = 0; self.sent = 0; self.fail_delete = False
                async def create_forum_topic(self, chat, subject): return {"message_thread_id": 77}
                async def send_message(self, chat, text, **kwargs): self.sent += 1; return [{"message_id": self.sent}]
                async def delete_forum_topic(self, chat, topic): self.deleted += 1; return not self.fail_delete

            raw = RawTelegram()
            projection = MailTelegramProjection(raw, -1001)
            self.assertEqual(await MailIngestionWorker(adapter, lambda _: _IMAP([mail]), projection).ingest_account(account), 1)
            await MailProjectionWorker(adapter, projection).run_once()
            row = repo.resolve_thread(account["id"], message_id="<incoming@test>")
            self.assertEqual(row["telegram_chat_id"], -1001)
            self.assertEqual(row["telegram_message_thread_id"], 77)
            conn = repo.db.connect()
            try:
                email_id = conn.execute("SELECT id FROM emails WHERE account_id = ? AND uid = '3'", (account["id"],)).fetchone()["id"]
            finally:
                conn.close()
            second = IncomingMail(account_id=account["id"], mailbox="INBOX", uid="4", message_id="<second@test>", sender="sender@example.com", to=("me@example.com",), subject="hello")
            self.assertTrue(adapter.insert_incoming_if_absent(second))
            adapter.assign_thread(second, row["id"])
            deleted = repo.enqueue_delete(account["id"], "delete-integrated", thread_id=row["id"])

            class Provider:
                def __init__(self): self.calls = []; self.fail_uid_four_once = True
                async def delete(self, mapping):
                    self.calls.append(mapping["uid"])
                    if mapping["uid"] == "4" and self.fail_uid_four_once:
                        self.fail_uid_four_once = False
                        return False
                    return mapping["uid"] in {"3", "4"}
            provider = Provider()
            worker = MailDeleteWorker(adapter, provider, projection)
            self.assertEqual((await worker.run_once(str(deleted["id"]))).state, "telegram_deleted")
            self.assertEqual(repo.get_delete(deleted["id"])["status"], "failed")
            with repo.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE delete_operations SET updated_at = 0 WHERE id = ?",
                    (deleted["id"],),
                )
            result = await worker.run_once(str(deleted["id"]))
            self.assertEqual(result.state, "tombstoned")
            self.assertEqual(provider.calls, ["3", "4", "4"])
            self.assertEqual(raw.deleted, 1)
            self.assertEqual(repo.get_delete(deleted["id"])["status"], "deleted")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_topic_success_before_phase_commit_recovers_from_missing_topic(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-topic-crash-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({
                "email": "crash@example.com", "imap_server": "imap.example.com",
                "imap_port": 993, "smtp_server": "smtp.example.com", "smtp_port": 465,
            }, "secret")
            incoming = repo.insert_incoming_if_absent(
                account["id"], mailbox="INBOX", uid="crash-topic", subject="Crash topic",
            )
            thread = repo.assign_thread(
                incoming["id"], telegram_chat_id=9001,
                telegram_message_thread_id=77, subject="Crash topic",
            )
            operation = repo.enqueue_delete(
                account["id"], "crash-topic-delete", thread_id=thread["id"],
            )
            # This marker is what the UI writes before making its detached Bot
            # request. It distinguishes an intentional delete from a stale
            # mapping that should remain strict.
            self.assertIsNotNone(repo.mark_delete_topic_requested(operation["id"]))
            adapter = V2MailRepositoryAdapter(repo)

            class Telegram:
                def __init__(self):
                    self.calls = 0

                async def delete_forum_topic(self, _chat_id, _thread_id):
                    self.calls += 1
                    if self.calls == 1:
                        return True
                    raise TelegramApiError(
                        "deleteForumTopic", "Bad Request: message thread not found"
                    )

            class Provider:
                def __init__(self):
                    self.calls = 0

                async def delete(self, _mapping):
                    self.calls += 1
                    return True

            telegram = Telegram()
            provider = Provider()
            projection = MailTelegramProjection(telegram, 9001)
            worker = MailDeleteWorker(adapter, provider, projection, worker_id="crashed")
            original_update = adapter.update_delete
            crashed = False

            def crash_after_topic(operation_id, state, **kwargs):
                nonlocal crashed
                if state == "telegram_deleted" and not crashed:
                    crashed = True
                    raise KeyboardInterrupt()
                return original_update(operation_id, state, **kwargs)

            adapter.update_delete = crash_after_topic
            with self.assertRaises(KeyboardInterrupt):
                await worker.run_once(str(operation["id"]))
            adapter.update_delete = original_update

            self.assertEqual(1, telegram.calls)
            self.assertEqual(0, provider.calls)
            self.assertEqual(0, repo.get_delete(operation["id"])["topic_deleted"])
            with repo.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE delete_operations SET lease_until = 0 WHERE id = ?",
                    (operation["id"],),
                )

            result = await MailDeleteWorker(
                adapter, provider, projection, worker_id="recovery",
            ).run_once(str(operation["id"]))

            self.assertEqual("tombstoned", result.state)
            self.assertEqual(2, telegram.calls)
            self.assertEqual(1, provider.calls)
            self.assertEqual("deleted", repo.get_delete(operation["id"])["status"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_real_v2_unbound_ingestion_replays_after_private_chat_binding(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-binding-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            mail = IncomingMail(account_id=account["id"], mailbox="INBOX", uid="awaiting", message_id="<awaiting@test>", sender="sender@example.com", to=("me@example.com",), subject="awaiting", text_body="body")

            class RawTelegram:
                def __init__(self): self.created = 0; self.sent = 0
                async def create_forum_topic(self, chat, subject): self.created += 1; return {"message_thread_id": 88}
                async def send_message(self, chat, text, **kwargs): self.sent += 1; return [{"message_id": 501}]

            raw = RawTelegram()
            projection = MailTelegramProjection(raw, lambda _: (repo.get_admin_binding() or {}).get("private_chat_id"))
            self.assertEqual(await MailIngestionWorker(adapter, lambda _: _IMAP([mail]), projection).ingest_account(account), 1)
            waiting = repo.scan_pending_projections(include_waiting=True)
            self.assertEqual(waiting[0]["status"], "waiting")
            self.assertEqual(raw.created, 0)
            repo.set_admin_private_chat(42, 9001)
            self.assertEqual(len(adapter.replay_pending_projections()), 1)
            await MailProjectionWorker(adapter, projection).run_once()
            conn = repo.db.connect()
            try:
                part = dict(conn.execute("SELECT * FROM telegram_delivery_parts WHERE email_id = (SELECT id FROM emails WHERE account_id = ? AND uid = 'awaiting')", (account["id"],)).fetchone())
            finally:
                conn.close()
            self.assertEqual(part["status"], "delivered")
            self.assertEqual(raw.created, 1)
            self.assertEqual(raw.sent, 1)
            thread = repo.resolve_thread(account["id"], message_id="<awaiting@test>")
            self.assertEqual(thread["telegram_chat_id"], 9001)
            self.assertEqual(thread["telegram_message_thread_id"], 88)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_deleted_existing_topic_is_replaced_and_whole_thread_is_requeued(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-topic-repair-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42, private_chat_id=9001)
            account = repo.create_account({
                "email": "me@example.com",
                "imap_server": "imap.example.com",
                "imap_port": 993,
                "smtp_server": "smtp.example.com",
                "smtp_port": 465,
            }, "secret")
            adapter = V2MailRepositoryAdapter(repo)

            class RawTelegram:
                def __init__(self):
                    self.topic_ids = iter((77, 88))
                    self.stale = False
                    self.message_id = 500

                async def create_forum_topic(self, chat, subject):
                    return {"message_thread_id": next(self.topic_ids)}

                async def send_message(self, chat, text, **kwargs):
                    topic_id = kwargs.get("message_thread_id")
                    if self.stale and topic_id == 77:
                        raise TelegramApiError(
                            "sendMessage", "Bad Request: message thread not found"
                        )
                    self.message_id += 1
                    return [{
                        "message_id": self.message_id,
                        "message_thread_id": topic_id,
                    }]

                async def delete_forum_topic(self, chat, topic):
                    return True

            raw = RawTelegram()
            projection = MailTelegramProjection(raw, 9001, repository=repo)
            worker = MailProjectionWorker(adapter, projection)
            first = IncomingMail(
                account_id=account["id"], mailbox="INBOX", uid="1",
                message_id="<first@test>", subject="same thread", text_body="first",
            )
            second = IncomingMail(
                account_id=account["id"], mailbox="INBOX", uid="2",
                message_id="<second@test>", subject="same thread", text_body="second",
            )

            await MailIngestionWorker(adapter, lambda _: _IMAP([first]), projection).ingest_account(account)
            await worker.run_once()
            raw.stale = True
            await MailIngestionWorker(adapter, lambda _: _IMAP([second]), projection).ingest_account(account)

            await worker.run_once()
            thread = repo.resolve_thread(account["id"], message_id="<first@test>")
            self.assertEqual(thread["telegram_message_thread_id"], 88)
            self.assertEqual(
                [part["status"] for part in repo.scan_pending_projections()],
                ["queued", "queued"],
            )

            await worker.run_once()
            await worker.run_once()
            conn = repo.db.connect()
            try:
                statuses = [row["status"] for row in conn.execute(
                    "SELECT status FROM telegram_delivery_parts ORDER BY id"
                ).fetchall()]
            finally:
                conn.close()
            self.assertEqual(statuses, ["delivered", "delivered"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_topic_replacement_only_queues_primary_parts_for_multi_attachment_mail(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-topic-attachments-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42, private_chat_id=9001)
            account = repo.create_account({
                "email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True,
            }, "secret")
            adapter = V2MailRepositoryAdapter(repo)

            class RawTelegram:
                def __init__(self):
                    self.fail = False
                    self.next_message_id = 500

                async def create_forum_topic(self, chat, subject):
                    return {"message_thread_id": 77}

                async def send_message(self, chat, text, **kwargs):
                    if self.fail:
                        raise ConnectionError("temporary replacement failure")
                    self.next_message_id += 1
                    return [{"message_id": self.next_message_id}]

                async def send_document(self, chat, document, **kwargs):
                    if self.fail:
                        raise ConnectionError("temporary replacement failure")
                    self.next_message_id += 1
                    return {"message_id": self.next_message_id}

            raw = RawTelegram()
            projection = MailTelegramProjection(raw, 9001, repository=repo)
            mail = IncomingMail(
                account_id=account["id"], mailbox="INBOX", uid="topic-attachments",
                message_id="<topic-attachments@test>", sender="sender@example.com", subject="Files",
                text_body="See files", attachments=(
                    Attachment("one.txt", b"one", "text/plain"),
                    Attachment("two.pdf", b"two", "application/pdf"),
                ),
            )
            self.assertEqual(
                await MailIngestionWorker(adapter, lambda _: _IMAP([mail]), projection).ingest_account(account),
                1,
            )
            await MailProjectionWorker(adapter, projection).run_once()
            thread = repo.resolve_thread(account["id"], message_id="<topic-attachments@test>")
            self.assertIsNotNone(thread)
            before = repo.list_telegram_delivery_parts(
                repo.get_email_by_imap_uid(account["id"], mailbox="INBOX", uid="topic-attachments")["id"],
                delivered_only=False,
            )
            self.assertEqual([part["part_index"] for part in before], [0, 1, 2])

            replaced = repo.replace_deleted_topic(
                thread["id"], expected_chat_id=9001, expected_message_thread_id=77,
                new_chat_id=9001, new_message_thread_id=88,
            )
            self.assertEqual(1, replaced["requeued_parts"])
            after = repo.list_telegram_delivery_parts(
                repo.get_email_by_imap_uid(account["id"], mailbox="INBOX", uid="topic-attachments")["id"],
                delivered_only=False,
            )
            self.assertEqual([(part["part_index"], part["status"]) for part in after], [(0, "queued")])

            raw.fail = True
            await MailProjectionWorker(adapter, projection).run_once()
            pending = repo.scan_pending_projections()
            self.assertEqual([(part["part_index"], part["status"]) for part in pending], [(0, "failed")])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_real_v2_replayed_projection_uses_persisted_llm_summary(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-summary-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            mail = IncomingMail(account_id=account["id"], mailbox="INBOX", uid="summary", sender="sender@example.com",
                                to=("me@example.com",), subject="private",
                                text_body="sensitive original email body https://example.test/tracker")

            class RawTelegram:
                def __init__(self): self.text = ""; self.markup = None
                async def create_forum_topic(self, chat, subject): return {"message_thread_id": 99}
                async def send_message(self, chat, text, **kwargs):
                    self.text += text
                    self.markup = kwargs.get("reply_markup") or self.markup
                    return [{"message_id": 502}]

            raw = RawTelegram()
            projection = MailTelegramProjection(raw, lambda _: (repo.get_admin_binding() or {}).get("private_chat_id"))
            self.assertEqual(await MailIngestionWorker(adapter, lambda _: _IMAP([mail]), projection,
                                                       llm_summarizer=lambda _: {
                                                           "summary": "durable safe summary", "category": "task", "priority": "high",
                                                           "important_links": [{"caption": "Open task", "link": "https://example.test/task"}],
                                                       }).ingest_account(account), 1)
            repo.set_admin_private_chat(42, 9001)
            # Simulate a process restart: replay must rebuild every mapping from
            # SQLite rather than rely on ingestion-time memory.
            adapter = V2MailRepositoryAdapter(repo)
            self.assertEqual(len(adapter.replay_pending_projections()), 1)
            await MailProjectionWorker(adapter, projection).run_once()
            conn = repo.db.connect()
            try:
                saved = conn.execute(
                    "SELECT llm_summary, llm_important_links_json FROM emails WHERE account_id = ? AND uid = 'summary'",
                    (account["id"],),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(saved["llm_summary"], "durable safe summary")
            self.assertIn("https://example.test/task", saved["llm_important_links_json"])
            self.assertIn("durable safe summary", raw.text)
            self.assertNotIn("sensitive original email body", raw.text)
            action_urls = [
                button["url"]
                for row in raw.markup["inline_keyboard"]
                for button in row
                if "url" in button
            ]
            self.assertEqual(["https://example.test/task"], action_urls)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_oversized_html_projection_persists_text_fallback_for_summary_refresh(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-html-fallback-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42, private_chat_id=9001)
            account = repo.create_account({
                "email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True,
            }, "secret")
            adapter = V2MailRepositoryAdapter(repo)

            class RawTelegram:
                def __init__(self):
                    self.edits = []

                async def create_forum_topic(self, chat, subject):
                    return {"message_thread_id": 99}

                async def send_document(self, chat, document, **kwargs):
                    return MiniAppOnlyProjection(len(document), kwargs.get("filename"))

                async def send_message(self, chat, text, **kwargs):
                    return [{"message_id": 601}]

                async def edit_message_text(self, chat, message_id, text, **kwargs):
                    self.edits.append(("text", chat, message_id, text, kwargs))

                async def edit_message_caption(self, chat, message_id, caption, **kwargs):
                    self.edits.append(("caption", chat, message_id, caption, kwargs))

            raw = RawTelegram()
            projection = MailTelegramProjection(raw, 9001, repository=repo)
            mail = IncomingMail(
                account_id=account["id"], mailbox="INBOX", uid="html-fallback",
                message_id="<html-fallback@test>", sender="sender@example.com", subject="Report",
                text_body="fallback", html_body="<p>original source</p>",
            )
            self.assertEqual(
                await MailIngestionWorker(adapter, lambda _: _IMAP([mail]), projection).ingest_account(account),
                1,
            )
            await MailProjectionWorker(adapter, projection).run_once()
            row = repo.get_email_by_imap_uid(account["id"], mailbox="INBOX", uid="html-fallback", uidvalidity="")
            self.assertIsNotNone(row)
            conn = repo.db.connect()
            try:
                part = conn.execute(
                    "SELECT message_kind FROM telegram_delivery_parts WHERE email_id = ?",
                    (int(row["id"]),),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(part["message_kind"], "text")

            refreshed = IncomingMail(
                account_id=account["id"], mailbox="INBOX", uid="html-fallback", email_id=row["id"],
                sender="sender@example.com", subject="Report", text_body="fallback",
                html_body="<p>original source</p>", summary="new summary",
            )
            self.assertTrue(await projection.update_mail_summary(refreshed))
            self.assertEqual(raw.edits[0][0], "text")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_projection_lease_expiry_is_ambiguous_until_explicit_requeue(self):
        from app.db import V2Repository
        fd, path = tempfile.mkstemp(prefix="telegramail-v2-projection-lease-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            repo.bind_admin(42, private_chat_id=9001)
            account = repo.create_account({"email": "me@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "imap_ssl": True, "smtp_server": "smtp.example.com", "smtp_port": 465, "smtp_ssl": True}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            mail = IncomingMail(account_id=account["id"], mailbox="INBOX", uid="lease", subject="lease")
            self.assertTrue(adapter.insert_incoming_if_absent(mail))
            self.assertTrue(adapter.enqueue_projection(mail))
            first = adapter.claim_next_projection("crashed-worker", 1)
            self.assertIsNotNone(first)
            self.assertTrue(first.lease_token)
            conn = repo.db.connect()
            try:
                conn.execute("UPDATE telegram_delivery_parts SET lease_until = 0 WHERE id = ?", (int(first.id),))
                conn.commit()
            finally:
                conn.close()
            # A potentially successful Telegram call is quarantined, never
            # automatically resent by a future worker claim.
            self.assertIsNone(adapter.claim_next_projection("new-worker", 60))
            conn = repo.db.connect()
            try:
                self.assertEqual(conn.execute("SELECT status FROM telegram_delivery_parts WHERE id = ?", (int(first.id),)).fetchone()["status"], "ambiguous")
            finally:
                conn.close()
            self.assertTrue(adapter.requeue_projection_after_reconciliation(first.id))
            second = adapter.claim_next_projection("reconciled-worker", 60)
            self.assertIsNotNone(second)
            self.assertNotEqual(first.lease_token, second.lease_token)
            self.assertTrue(adapter.complete_projection(second.id, True, lease_token=second.lease_token))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_no_message_id_threads_do_not_share_null_root_and_normalize_subject(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-no-message-id-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "no-id@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "smtp_server": "smtp.example.com", "smtp_port": 465}, "secret")
            first = repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="1", subject="Alpha")
            second = repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="2", subject="Beta")
            first_thread = repo.assign_thread(first["id"], telegram_chat_id=9001, telegram_message_thread_id=11, subject="Alpha")
            second_thread = repo.assign_thread(second["id"], telegram_chat_id=9001, telegram_message_thread_id=12, subject="Beta")
            self.assertNotEqual(first_thread["id"], second_thread["id"])
            self.assertEqual(first_thread["subject_normalized"], "alpha")
            reply = repo.resolve_thread(account["id"], subject="Re： Alpha")
            self.assertEqual(reply["id"], first_thread["id"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_thread_delete_with_only_outgoing_history_skips_provider(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-outgoing-delete-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "outgoing@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "smtp_server": "smtp.example.com", "smtp_port": 465}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            incoming = repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="1", message_id="<incoming@test>", subject="sent")
            thread = repo.assign_thread(incoming["id"], telegram_chat_id=9001, telegram_message_thread_id=55, subject="sent")
            repo.insert_outgoing_email(account["id"], thread_id=thread["id"], message_id="<outgoing@test>", subject="sent", sender="me@example.com")
            repo.tombstone_email(incoming["id"])
            operation = repo.enqueue_delete(account["id"], "outgoing-only-delete", thread_id=thread["id"])

            class Provider:
                async def delete(self, _mapping):
                    raise AssertionError("there should be no provider target")

            class Telegram:
                async def delete_forum_topic(self, _chat, _topic):
                    return True

            result = await MailDeleteWorker(adapter, Provider(), Telegram()).run_once(str(operation["id"]))
            self.assertEqual(result.state, "tombstoned")
            self.assertEqual(repo.get_delete(operation["id"])["status"], "deleted")
            conn = repo.db.connect()
            try:
                self.assertIsNotNone(conn.execute("SELECT tombstoned_at FROM emails WHERE message_id = '<outgoing@test>'").fetchone()["tombstoned_at"])
            finally:
                conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_threadless_delete_tombstones_local_email(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-threadless-delete-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "threadless@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "smtp_server": "smtp.example.com", "smtp_port": 465}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            incoming = repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="9", message_id="<threadless@test>", subject="one")
            operation = repo.enqueue_delete(account["id"], "threadless-delete", email_id=incoming["id"])

            class Provider:
                async def delete(self, _mapping):
                    return True

            class Telegram:
                async def delete_forum_topic(self, *_args):
                    raise AssertionError("threadless operation has no topic")

            result = await MailDeleteWorker(adapter, Provider(), Telegram()).run_once(str(operation["id"]))
            self.assertEqual(result.state, "tombstoned")
            conn = repo.db.connect()
            try:
                row = conn.execute("SELECT tombstoned_at FROM emails WHERE id = ?", (incoming["id"],)).fetchone()
                self.assertIsNotNone(row["tombstoned_at"])
            finally:
                conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_threadless_tombstone_requires_current_delete_lease(self):
        from app.db import V2Repository

        fd, path = tempfile.mkstemp(prefix="telegramail-v2-threadless-lease-race-", suffix=".db")
        os.close(fd)
        try:
            repo = V2Repository(path, master_key=b"x" * 32)
            account = repo.create_account({"email": "threadless-lease@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                                           "smtp_server": "smtp.example.com", "smtp_port": 465}, "secret")
            adapter = V2MailRepositoryAdapter(repo)
            incoming = repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="10", subject="one")
            operation = repo.enqueue_delete(account["id"], "threadless-lease-race", email_id=incoming["id"])

            repo.claim_delete(operation["id"], lease_token="old-worker", lease_seconds=60)
            with repo.db.transaction(immediate=True) as conn:
                conn.execute("UPDATE delete_operations SET lease_until = 0 WHERE id = ?", (operation["id"],))
            repo.claim_delete(operation["id"], lease_token="new-worker", lease_seconds=60)
            repo.update_delete(operation["id"], provider_deleted=True, lease_token="new-worker")
            repo.update_delete(operation["id"], topic_deleted=True, lease_token="new-worker")

            # An expired worker must not tombstone the local row before its
            # operation CAS fails.
            self.assertFalse(adapter.tombstone_email(
                incoming["id"], operation["id"], lease_token="old-worker",
            ))
            conn = repo.db.connect()
            try:
                self.assertIsNone(conn.execute(
                    "SELECT tombstoned_at FROM emails WHERE id = ?", (incoming["id"],)
                ).fetchone()["tombstoned_at"])
            finally:
                conn.close()
            self.assertEqual("deleting", repo.get_delete(operation["id"])["status"])

            self.assertTrue(adapter.tombstone_email(
                incoming["id"], operation["id"], lease_token="new-worker",
            ))
            self.assertEqual("deleted", repo.get_delete(operation["id"])["status"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass
