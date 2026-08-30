import tempfile
import unittest
import sqlite3
import json
import time
from pathlib import Path
from unittest import mock

from app.db import V2Repository


class V2RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = V2Repository(Path(self.tmp.name) / "v2.sqlite", master_key=b"k" * 32)
        self.account = self.repo.create_account(
            {
                "email": "owner@example.com", "imap_server": "imap.example.com", "imap_port": 993,
                "smtp_server": "smtp.example.com", "smtp_port": 465,
            },
            "not stored in accounts",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_secret_is_encrypted_and_contacts_are_ranked(self):
        self.assertEqual("not stored in accounts", self.repo.get_account_password(self.account["id"]))
        conn = self.repo.db.connect()
        try:
            self.assertNotIn("password", {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")})
            stored = conn.execute("SELECT ciphertext FROM account_secrets WHERE account_id = ?", (self.account["id"],)).fetchone()["ciphertext"]
            self.assertNotIn(b"not stored in accounts", stored)
        finally:
            conn.close()
        self.repo.upsert_contact(self.account["id"], "other@example.com", seen_at=1)
        self.repo.upsert_contact(self.account["id"], "owner@example.com", seen_at=2)
        result = self.repo.search_contacts(self.account["id"], "owner")
        self.assertEqual("owner@example.com", result[0]["email"])

    def test_operations_are_idempotent_and_leased(self):
        first = self.repo.enqueue_send(self.account["id"], "request-1")
        duplicate = self.repo.enqueue_send(self.account["id"], "request-1")
        self.assertEqual(first["id"], duplicate["id"])
        claimed = self.repo.claim_send(first["id"], lease_token="worker-a")
        self.assertEqual("sending", claimed["status"])
        self.assertIsNone(self.repo.claim_send(first["id"], lease_token="worker-b"))
        self.assertIsNone(self.repo.complete_send(first["id"], lease_token="worker-b", success=True, provider_message_id="m-1"))
        finished = self.repo.complete_send(first["id"], lease_token="worker-a", success=True, provider_message_id="m-1")
        self.assertEqual("sent", finished["status"])

        deletion = self.repo.enqueue_delete(self.account["id"], "delete-1")
        self.repo.claim_delete(deletion["id"], lease_token="worker-a")
        with self.assertRaises(ValueError):
            self.repo.update_delete(deletion["id"], success=True)
        staged = self.repo.update_delete(deletion["id"], provider_deleted=True)
        self.assertEqual(1, staged["provider_deleted"])
        deleted = self.repo.update_delete(deletion["id"], success=True, topic_deleted=True, tombstoned=True)
        self.assertEqual("deleted", deleted["status"])

    def test_incoming_insert_and_thread_assignment_are_idempotent(self):
        message = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="42", message_id="<one@example.com>", subject="Hello")
        again = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="42", message_id="ignored")
        self.assertEqual(message["id"], again["id"])
        self.assertTrue(message["is_new"])
        self.assertFalse(again["is_new"])
        thread = self.repo.assign_thread(message["id"], telegram_chat_id=123, telegram_message_thread_id=456)
        self.assertEqual(thread["id"], self.repo.resolve_thread(self.account["id"], message_id="<one@example.com>")["id"])
        deletion = self.repo.enqueue_delete(self.account["id"], "delete-target", email_id=message["id"])
        target = self.repo.get_delete_target(deletion["id"])
        self.assertEqual({"account_id": self.account["id"], "provider_mailbox": "INBOX", "provider_uid": "42", "telegram_chat_id": 123, "telegram_message_thread_id": 456},
                         {key: target[key] for key in ("account_id", "provider_mailbox", "provider_uid", "telegram_chat_id", "telegram_message_thread_id")})
        self.assertEqual([{"account_id": self.account["id"], "email_id": message["id"], "provider_mailbox": "INBOX", "provider_uid": "42"}],
                         [{key: item[key] for key in ("account_id", "email_id", "provider_mailbox", "provider_uid")} for item in target["provider_mappings"]])

    def test_llm_labels_and_max_uid_are_persisted(self):
        first = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="9", subject="Tagged",
                                                    llm_category="finance", llm_priority="high", llm_confidence=0.8, llm_labeled_at=100)
        self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="11", subject="Later")
        self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="outgoing:x", subject="Synthetic")
        updated = self.repo.update_email_llm_labels(email_id=first["id"], category="Finance", priority="high", confidence=2, labeled_at=200, summary="Durable summary")
        self.assertEqual(("finance", "high", 1.0, 200, "Durable summary"), (updated["llm_category"], updated["llm_priority"], updated["llm_confidence"], updated["llm_labeled_at"], updated["llm_summary"]))
        self.assertEqual("Durable summary", self.repo.get_email(first["id"])["llm_summary"])
        self.assertEqual(11, self.repo.get_max_uid(self.account["id"]))

    def test_llm_important_links_are_persisted_as_json(self):
        first = self.repo.insert_incoming_if_absent(
            self.account["id"], mailbox="INBOX", uid="links", subject="Links"
        )
        links = [
            {"caption": "Open invoice", "link": "https://example.test/invoice"},
            {"caption": "Dashboard", "link": "https://example.test/dashboard"},
        ]
        updated = self.repo.update_email_llm_labels(
            email_id=first["id"], category="other", priority="medium", important_links=links
        )
        self.assertEqual(links, updated["important_links"])
        self.assertEqual(links, self.repo.get_email(first["id"])["important_links"])
        conn = self.repo.db.connect()
        try:
            raw = conn.execute("SELECT llm_important_links_json FROM emails WHERE id = ?", (first["id"],)).fetchone()[0]
            self.assertEqual(links, json.loads(raw))
        finally:
            conn.close()

    def test_imap_cursor_bootstraps_and_uidvalidity_requires_reset(self):
        self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="7", subject="Old")
        self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="12", subject="Newest")
        bootstrapped = self.repo.get_or_bootstrap_imap_cursor(self.account["id"], "INBOX")
        self.assertEqual((None, 12, True), (bootstrapped["uidvalidity"], bootstrapped["last_uid"], bootstrapped["bootstrapped"]))
        advanced = self.repo.advance_imap_cursor(self.account["id"], "INBOX", "100", 15)
        self.assertEqual(("100", 15, False), (advanced["uidvalidity"], advanced["last_uid"], advanced["reset_required"]))
        self.assertEqual(15, self.repo.advance_imap_cursor(self.account["id"], "INBOX", "100", 14)["last_uid"])
        mismatch = self.repo.advance_imap_cursor(self.account["id"], "INBOX", "200", 1)
        self.assertTrue(mismatch["reset_required"])
        self.assertEqual(("100", 15), (mismatch["uidvalidity"], mismatch["last_uid"]))
        reset = self.repo.reset_imap_cursor(self.account["id"], "INBOX", "200")
        self.assertEqual(("200", 0, True), (reset["uidvalidity"], reset["last_uid"], reset["reset"]))

    def test_uidvalidity_is_part_of_incoming_message_identity(self):
        first = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="88", uidvalidity="epoch-a", subject="A")
        duplicate = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="88", uidvalidity="epoch-a", subject="ignored")
        epoch_b = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="88", uidvalidity="epoch-b", subject="B")
        self.assertEqual((True, False, True), (first["is_new"], duplicate["is_new"], epoch_b["is_new"]))
        self.assertNotEqual(first["id"], epoch_b["id"])
        self.assertEqual(88, self.repo.get_max_uid(self.account["id"], uidvalidity="epoch-b"))
        self.assertEqual(epoch_b["id"], self.repo.get_email_by_imap_uid(self.account["id"], mailbox="INBOX", uid="88", uidvalidity="epoch-b")["id"])

    def test_expired_operation_leases_are_reclaimable(self):
        send = self.repo.enqueue_send(self.account["id"], "expired-send")
        self.repo.claim_send(send["id"], lease_token="old-send", lease_seconds=60)
        deletion = self.repo.enqueue_delete(self.account["id"], "expired-delete")
        self.repo.claim_delete(deletion["id"], lease_token="old-delete", lease_seconds=60)
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE send_operations SET lease_until = 0 WHERE id = ?", (send["id"],))
            conn.execute("UPDATE delete_operations SET lease_until = 0 WHERE id = ?", (deletion["id"],))
        reclaimed_send = self.repo.claim_send(send["id"], lease_token="new-send")
        reclaimed_delete = self.repo.claim_next_delete(lease_token="new-delete")
        self.assertEqual(("sending", "new-send", 2), (reclaimed_send["status"], reclaimed_send["lease_token"], reclaimed_send["attempt_count"]))
        self.assertEqual(("deleting", "new-delete", 2), (reclaimed_delete["status"], reclaimed_delete["lease_token"], reclaimed_delete["attempt_count"]))

    def test_new_queued_delete_is_not_starved_by_an_older_failure(self):
        failed = self.repo.enqueue_delete(self.account["id"], "failed-delete")
        self.repo.claim_delete(failed["id"], lease_token="failed-worker")
        self.repo.update_delete(
            failed["id"], success=False, error_message="provider unavailable"
        )
        queued = self.repo.enqueue_delete(self.account["id"], "queued-delete")

        claimed = self.repo.claim_next_delete(lease_token="next-worker")

        self.assertEqual(queued["id"], claimed["id"])
        self.assertEqual("deleting", claimed["status"])

    def test_failed_delete_waits_before_background_retry(self):
        failed = self.repo.enqueue_delete(self.account["id"], "backoff-delete")
        self.repo.claim_delete(failed["id"], lease_token="failed-worker")
        self.repo.update_delete(failed["id"], success=False, error_message="provider unavailable")

        self.assertIsNone(self.repo.claim_next_delete(lease_token="too-soon"))

        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE delete_operations SET updated_at = 0 WHERE id = ?", (failed["id"],))
        retried = self.repo.claim_next_delete(lease_token="after-backoff")
        self.assertEqual(("deleting", "after-backoff"), (retried["status"], retried["lease_token"]))

    def test_failed_delete_backoff_doubles_and_caps(self):
        failed = self.repo.enqueue_delete(self.account["id"], "exponential-backoff-delete")
        self.repo.claim_delete(failed["id"], lease_token="first-worker")
        self.repo.update_delete(failed["id"], success=False, error_message="provider unavailable")

        now = int(time.time())
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE delete_operations SET updated_at = ? WHERE id = ?",
                (now - 31, failed["id"]),
            )
        second = self.repo.claim_next_delete(lease_token="second-worker")
        self.assertEqual(("deleting", "second-worker", 2), (second["status"], second["lease_token"], second["attempt_count"]))
        self.repo.update_delete(failed["id"], success=False, error_message="provider unavailable")

        # The second failure waits 60 seconds, not the original fixed 30.
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE delete_operations SET updated_at = ? WHERE id = ?",
                (int(time.time()) - 31, failed["id"]),
            )
        self.assertIsNone(self.repo.claim_next_delete(lease_token="too-soon-second"))
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE delete_operations SET updated_at = ? WHERE id = ?",
                (int(time.time()) - 61, failed["id"]),
            )
        second_retry = self.repo.claim_next_delete(lease_token="after-second-backoff")
        self.assertEqual(("deleting", "after-second-backoff", 3), (second_retry["status"], second_retry["lease_token"], second_retry["attempt_count"]))

        self.repo.update_delete(failed["id"], success=False, error_message="provider unavailable")
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE delete_operations SET attempt_count = 100, updated_at = ? WHERE id = ?",
                (int(time.time()) - 3599, failed["id"]),
            )
        self.assertIsNone(self.repo.claim_next_delete(lease_token="too-soon-cap"))
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE delete_operations SET updated_at = ? WHERE id = ?",
                (int(time.time()) - 3600, failed["id"]),
            )
        capped = self.repo.claim_next_delete(lease_token="after-cap")
        self.assertEqual(("deleting", "after-cap"), (capped["status"], capped["lease_token"]))

    def test_thread_delete_targets_resume_and_tombstone_atomically(self):
        first = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="thread-1", subject="Topic")
        second = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="thread-2", subject="Topic")
        thread = self.repo.assign_thread(first["id"], telegram_chat_id=9, telegram_message_thread_id=10)
        self.repo.assign_thread(second["id"], thread_id=thread["id"])
        operation = self.repo.enqueue_delete(self.account["id"], "thread-delete", thread_id=thread["id"])
        pending = self.repo.list_delete_targets(operation["id"], pending_only=True)
        self.assertEqual([first["id"], second["id"]], [item["email_id"] for item in pending])
        self.assertEqual([], self.repo.list_telegram_inbox_threads()["items"])
        self.repo.claim_delete(operation["id"], lease_token="failed-delete")
        self.repo.update_delete(operation["id"], success=False, error_message="provider unavailable")
        # A failed operation is still retryable, so its optimistic removal must
        # not make the thread reappear while the backoff timer is running.
        self.assertEqual([], self.repo.list_telegram_inbox_threads()["items"])
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE delete_operations SET updated_at = 0 WHERE id = ?", (operation["id"],))
        self.repo.claim_delete(operation["id"], lease_token="delete")
        self.repo.mark_delete_target_provider_deleted(operation["id"], first["id"], success=True)
        self.assertEqual([second["id"]], [item["email_id"] for item in self.repo.list_delete_targets(operation["id"], pending_only=True)])
        self.repo.mark_delete_target_provider_deleted(operation["id"], second["id"], success=True)
        claimed = self.repo.get_delete(operation["id"])
        self.assertEqual("deleting", claimed["status"])
        self.repo.update_delete(operation["id"], topic_deleted=True)
        final = self.repo.tombstone_thread(thread["id"], delete_operation_id=operation["id"])
        self.assertEqual(("deleted", 1), (final["status"], final["tombstoned"]))
        self.assertIsNone(self.repo.resolve_thread(self.account["id"], subject="Topic"))
        later = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="thread-after-delete", subject="Topic")
        replacement = self.repo.assign_thread(later["id"])
        self.assertNotEqual(thread["id"], replacement["id"])
        self.assertEqual("active", replacement["status"])

    def test_delete_freezes_thread_before_later_mail_can_be_projected(self):
        first = self.repo.insert_incoming_if_absent(
            self.account["id"], mailbox="INBOX", uid="freeze-1",
            message_id="<freeze-1@test>", subject="Same subject",
        )
        thread = self.repo.assign_thread(
            first["id"], telegram_chat_id=9, telegram_message_thread_id=10,
        )
        operation = self.repo.enqueue_delete(
            self.account["id"], "freeze-delete", thread_id=thread["id"],
        )

        later = self.repo.insert_incoming_if_absent(
            self.account["id"], mailbox="INBOX", uid="freeze-2",
            message_id="<freeze-2@test>", in_reply_to="<freeze-1@test>",
            subject="Same subject",
        )
        self.assertIsNone(
            self.repo.resolve_thread(
                self.account["id"], in_reply_to="<freeze-1@test>",
                subject="Same subject",
            )
        )
        replacement = self.repo.assign_thread(
            later["id"], root_message_id=later["message_id"],
            subject=later["subject"],
        )

        self.assertNotEqual(thread["id"], replacement["id"])
        conn = self.repo.db.connect()
        try:
            self.assertEqual("deleting", conn.execute(
                "SELECT status FROM mail_threads WHERE id = ?", (thread["id"],)
            ).fetchone()["status"])
        finally:
            conn.close()
        self.assertEqual(
            [first["id"]],
            [item["email_id"] for item in self.repo.list_delete_targets(operation["id"])],
        )

    def test_delete_phase_flags_are_monotonic_and_stale_lease_cannot_commit(self):
        email = self.repo.insert_incoming_if_absent(
            self.account["id"], mailbox="INBOX", uid="lease-delete",
        )
        operation = self.repo.enqueue_delete(
            self.account["id"], "lease-delete", email_id=email["id"],
        )
        self.repo.claim_delete(operation["id"], lease_token="old", lease_seconds=60)
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE delete_operations SET lease_until = 0 WHERE id = ?", (operation["id"],))
        self.repo.claim_delete(operation["id"], lease_token="new", lease_seconds=60)

        self.assertIsNone(
            self.repo.update_delete(
                operation["id"], topic_deleted=True, lease_token="old",
            )
        )
        staged = self.repo.update_delete(
            operation["id"], topic_deleted=True, lease_token="new",
        )
        self.assertEqual(1, staged["topic_deleted"])
        regressed = self.repo.update_delete(
            operation["id"], topic_deleted=False, lease_token="new",
        )
        self.assertEqual(1, regressed["topic_deleted"])

    def test_stale_topic_mark_cannot_clear_provider_phase(self):
        email = self.repo.insert_incoming_if_absent(
            self.account["id"], mailbox="INBOX", uid="phase-race",
        )
        operation = self.repo.enqueue_delete(
            self.account["id"], "phase-race", email_id=email["id"],
        )
        self.repo.claim_delete(operation["id"], lease_token="worker")
        stale_snapshot = dict(self.repo.get_delete(operation["id"]))
        self.repo.update_delete(
            operation["id"], provider_deleted=True, lease_token="worker",
        )

        # The UI has no worker lease and can race with a worker using a stale
        # snapshot of the phase flags. SQL-level MAX merging must preserve the
        # provider phase that was completed after that snapshot was read.
        with mock.patch.object(self.repo, "get_delete", return_value=stale_snapshot):
            marked = self.repo.mark_delete_topic_deleted(operation["id"])
        self.assertEqual((1, 1), (marked["provider_deleted"], marked["topic_deleted"]))

    def test_ui_delete_markers_do_not_rewind_claimed_operation(self):
        email = self.repo.insert_incoming_if_absent(
            self.account["id"], mailbox="INBOX", uid="marker-race",
        )
        operation = self.repo.enqueue_delete(
            self.account["id"], "marker-race", email_id=email["id"],
        )
        stale_snapshot = dict(self.repo.get_delete(operation["id"]))
        self.repo.claim_delete(operation["id"], lease_token="worker")

        # A UI callback can be based on a queued snapshot while a worker has
        # already claimed the operation. Marker persistence must not rewrite
        # the worker's status or lease (and must not move its retry clock).
        with mock.patch.object(self.repo, "get_delete", return_value=stale_snapshot):
            requested = self.repo.mark_delete_topic_requested(operation["id"])
        self.assertEqual(("deleting", "worker", 1), (
            requested["status"], requested["lease_token"], requested["topic_delete_requested"],
        ))

    def test_waiting_projection_replays_after_private_chat_binding(self):
        message = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="projection", subject="Queued")
        waiting = self.repo.mark_projection_waiting(message["id"])
        self.assertEqual("waiting", waiting["status"])
        self.repo.bind_admin(700)
        binding = self.repo.set_admin_private_chat(700, 701)
        self.assertEqual(701, binding["private_chat_id"])
        replayed = self.repo.replay_pending_projections()
        self.assertEqual(1, len(replayed))
        self.assertEqual(("queued", 701), (replayed[0]["status"], replayed[0]["telegram_chat_id"]))
        claimed = self.repo.claim_next_projection()
        self.assertEqual("delivering", claimed["status"])
        delivered = self.repo.complete_projection(
            claimed["id"], lease_token=claimed["lease_token"], success=True,
            telegram_message_id=55, message_kind="text",
        )
        self.assertEqual(("delivered", 55, "text"), (delivered["status"], delivered["telegram_message_id"], delivered["message_kind"]))

    def test_expired_projection_lease_becomes_ambiguous_until_reconciled(self):
        email = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="projection-expiry", subject="Projection")
        self.repo.bind_admin(800, private_chat_id=801)
        part = self.repo.mark_projection_waiting(email["id"])
        claimed = self.repo.claim_projection(part["id"], lease_token="first", lease_seconds=60)
        with self.repo.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE telegram_delivery_parts SET lease_until = 0 WHERE id = ?", (claimed["id"],))
        ambiguous = self.repo.reconcile_expired_projections()
        self.assertEqual((1, "ambiguous"), (len(ambiguous), ambiguous[0]["status"]))
        self.assertIsNone(self.repo.claim_next_projection(lease_token="second"))
        self.assertIsNone(self.repo.complete_projection(claimed["id"], lease_token="first", success=True, telegram_message_id=1))
        self.repo.requeue_projection_after_reconciliation(claimed["id"])
        reclaimed = self.repo.claim_next_projection(lease_token="second")
        self.assertEqual(("delivering", "second", 2), (reclaimed["status"], reclaimed["lease_token"], reclaimed["attempts"]))

    def test_fresh_projection_is_not_starved_by_an_older_failure(self):
        self.repo.bind_admin(900, private_chat_id=901)
        first_email = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="failed-first")
        first_part = self.repo.mark_projection_waiting(first_email["id"])
        first_claim = self.repo.claim_projection(first_part["id"], lease_token="first")
        self.repo.complete_projection(first_claim["id"], lease_token="first", success=False)

        second_email = self.repo.insert_incoming_if_absent(self.account["id"], mailbox="INBOX", uid="fresh-second")
        second_part = self.repo.mark_projection_waiting(second_email["id"])
        next_claim = self.repo.claim_next_projection(lease_token="next")

        self.assertEqual(second_part["id"], next_claim["id"])

    def test_early_v2_delivery_table_is_upgraded_without_data_loss(self):
        old_path = Path(self.tmp.name) / "early-v2.sqlite"
        conn = sqlite3.connect(old_path)
        conn.executescript("""
            CREATE TABLE emails (id INTEGER PRIMARY KEY, account_id INTEGER, thread_id INTEGER, message_id TEXT);
            CREATE TABLE telegram_delivery_parts (
                id INTEGER PRIMARY KEY, email_id INTEGER NOT NULL, part_index INTEGER NOT NULL,
                telegram_chat_id INTEGER NOT NULL, telegram_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                UNIQUE(email_id, part_index)
            );
            INSERT INTO emails(id) VALUES (1);
            INSERT INTO telegram_delivery_parts VALUES (1, 1, 0, 99, NULL, 'pending', 1, 1);
        """)
        conn.commit()
        conn.close()
        upgraded = V2Repository(old_path, master_key=b"k" * 32)
        conn = upgraded.db.connect()
        try:
            row = conn.execute("SELECT telegram_chat_id, status FROM telegram_delivery_parts").fetchone()
            self.assertEqual((99, "queued"), (row["telegram_chat_id"], row["status"]))
            self.assertIsNone(conn.execute("SELECT llm_summary FROM emails WHERE id = 1").fetchone()["llm_summary"])
            self.assertEqual("[]", conn.execute("SELECT llm_important_links_json FROM emails WHERE id = 1").fetchone()["llm_important_links_json"])
        finally:
            conn.close()
