import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.migrate_v2 import initialize_empty_v2, migrate, upgrade_existing_v2, validate_v2
from app.db.schema import initialize_schema


class V2MigrationTests(unittest.TestCase):
    def test_schema_upgrade_rolls_back_if_an_alter_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path, isolation_level=None)
            conn.executescript("""
                CREATE TABLE accounts (id INTEGER PRIMARY KEY);
                CREATE TABLE emails (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER,
                    mailbox TEXT,
                    uid TEXT
                );
                INSERT INTO accounts(id) VALUES (1);
                INSERT INTO emails(id, account_id, mailbox, uid) VALUES (1, 1, 'INBOX', '7');
            """)

            def deny_alter(action, _arg1, _arg2, _db, _trigger):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK

            conn.set_authorizer(deny_alter)
            with self.assertRaises(sqlite3.DatabaseError):
                initialize_schema(conn)
            conn.set_authorizer(None)
            self.assertEqual(conn.execute("SELECT uid FROM emails WHERE id = 1").fetchone()[0], "7")
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'emails__epoch_new'").fetchone())
            conn.close()

    def test_schema_upgrade_recovers_a_pre_atomic_temp_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.db"
            conn = sqlite3.connect(path, isolation_level=None)
            initialize_schema(conn)
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("INSERT INTO accounts(email, imap_server, imap_port, smtp_server, smtp_port, created_at, updated_at) VALUES ('a@example.test', 'imap', 993, 'smtp', 465, 1, 1)")
            conn.execute("INSERT INTO emails(account_id, mailbox, uidvalidity, uid, created_at, updated_at) VALUES (1, 'INBOX', 'epoch', '9', 1, 1)")
            conn.execute("ALTER TABLE emails RENAME TO emails__epoch_new")

            initialize_schema(conn)

            self.assertEqual(conn.execute("SELECT uid FROM emails").fetchone()[0], "9")
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'emails__epoch_new'").fetchone())
            conn.close()

    def test_migration_preserves_orphans_without_telegram_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, key = root / "old.sqlite", root / "v2.sqlite", root / "key"
            key.write_bytes(b"x" * 32)
            old = sqlite3.connect(source)
            old.executescript("""
                CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT, password TEXT, imap_server TEXT, imap_port INTEGER, imap_ssl INTEGER, smtp_server TEXT, smtp_port INTEGER, smtp_ssl INTEGER, alias TEXT, signature TEXT);
                CREATE TABLE emails (id INTEGER PRIMARY KEY, email_account INTEGER, mailbox TEXT, uid TEXT, sender TEXT, subject TEXT, telegram_thread_id TEXT);
                CREATE TABLE drafts (id INTEGER PRIMARY KEY, account_id INTEGER, draft_type TEXT, from_identity_email TEXT, status TEXT, to_addrs TEXT);
                CREATE TABLE draft_attachments (id INTEGER PRIMARY KEY, draft_id INTEGER, file_id INTEGER, file_name TEXT);
                CREATE TABLE deleted_topics (chat_id INTEGER, thread_id TEXT);
                INSERT INTO accounts VALUES (1, 'a@example.com', 'secret', 'imap', 993, 1, 'smtp', 465, 1, 'A', 'Signed');
                INSERT INTO emails VALUES (1, 1, 'INBOX', '1', 'sender@example.com', 'regular', '99');
                INSERT INTO emails VALUES (2, 99, 'INBOX', '2', 'sender@example.com', 'orphan', '100');
                INSERT INTO drafts VALUES (1, 1, 'compose', 'a@example.com', 'open', 'to@example.com');
                INSERT INTO draft_attachments VALUES (1, 1, 100, 'file.txt');
                ALTER TABLE emails ADD COLUMN llm_category TEXT;
                ALTER TABLE emails ADD COLUMN llm_priority TEXT;
                ALTER TABLE emails ADD COLUMN llm_confidence REAL;
                ALTER TABLE emails ADD COLUMN llm_labeled_at INTEGER;
                ALTER TABLE emails ADD COLUMN llm_summary TEXT;
                UPDATE emails SET llm_category = 'finance', llm_priority = 'high', llm_confidence = 0.9, llm_labeled_at = 1700000000, llm_summary = 'legacy durable summary' WHERE id = 1;
                INSERT INTO emails(id, email_account, mailbox, uid, sender, subject, telegram_thread_id) VALUES (3, 1, 'INBOX', '1', 'sender@example.com', 'duplicate uid', '101');
            """)
            old.commit()
            old.close()
            previous = os.environ.get("MASTER_KEY_FILE")
            os.environ["MASTER_KEY_FILE"] = str(key)
            try:
                dry = migrate(source, root / "dry.sqlite", dry_run=True)
                self.assertFalse((root / "dry.sqlite").exists())
                report = migrate(source, target)
            finally:
                if previous is None:
                    os.environ.pop("MASTER_KEY_FILE", None)
                else:
                    os.environ["MASTER_KEY_FILE"] = previous
            self.assertEqual(1, report["orphan_emails_migrated"])
            self.assertEqual(1, report["migrated_counts"]["llm_labeled_emails"])
            self.assertEqual({"emails": 1}, report["skipped"])
            self.assertEqual({"emails": 1}, report["conflicts"])
            self.assertEqual("ok", report["integrity_check"])
            migrated = sqlite3.connect(target)
            self.assertEqual(2, migrated.execute("SELECT COUNT(*) FROM emails").fetchone()[0])
            self.assertEqual(1, migrated.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 0").fetchone()[0])
            self.assertEqual(("archived", 1), migrated.execute("SELECT status, migration_hold FROM drafts").fetchone())
            self.assertEqual("Signed", migrated.execute("SELECT signature FROM accounts WHERE email = 'a@example.com'").fetchone()[0])
            self.assertEqual("legacy_missing", migrated.execute("SELECT availability FROM draft_attachments").fetchone()[0])
            self.assertEqual(("finance", "high", 0.9, 1700000000, "legacy durable summary"), migrated.execute("SELECT llm_category, llm_priority, llm_confidence, llm_labeled_at, llm_summary FROM emails WHERE uid = '1'").fetchone())
            self.assertEqual((2, 2), migrated.execute("SELECT usage_count, frequency FROM contacts WHERE email = 'sender@example.com' AND account_id = 1").fetchone())
            self.assertEqual(0, migrated.execute("SELECT COUNT(*) FROM telegram_delivery_parts").fetchone()[0])
            migrated.close()

    def test_explicit_empty_init_requires_new_target_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, key = root / "v2.sqlite", root / "key"
            key.write_bytes(b"z" * 32)
            previous = os.environ.get("MASTER_KEY_FILE")
            os.environ["MASTER_KEY_FILE"] = str(key)
            try:
                report = initialize_empty_v2(target)
                self.assertTrue(report["initialized"])
                with self.assertRaises(ValueError):
                    initialize_empty_v2(target)
            finally:
                if previous is None:
                    os.environ.pop("MASTER_KEY_FILE", None)
                else:
                    os.environ["MASTER_KEY_FILE"] = previous

    def test_upgrade_existing_v2_repairs_an_incomplete_schema_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, key = root / "v2.sqlite", root / "key"
            key.write_bytes(b"u" * 32)
            previous = os.environ.get("MASTER_KEY_FILE")
            os.environ["MASTER_KEY_FILE"] = str(key)
            try:
                initialize_empty_v2(target)
                conn = sqlite3.connect(target)
                conn.execute(
                    "INSERT INTO accounts(email, imap_server, imap_port, smtp_server, smtp_port, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("a@example.test", "imap.example.test", 993, "smtp.example.test", 465, 1, 1),
                )
                conn.execute("DROP TABLE summary_tasks")
                conn.commit()
                conn.close()

                report = upgrade_existing_v2(target)
            finally:
                if previous is None:
                    os.environ.pop("MASTER_KEY_FILE", None)
                else:
                    os.environ["MASTER_KEY_FILE"] = previous

            self.assertTrue(report["upgraded"])
            repaired = sqlite3.connect(target)
            self.assertEqual(1, repaired.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            self.assertIsNotNone(repaired.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'summary_tasks'").fetchone())
            repaired.close()

    def test_schema_upgrade_queues_legacy_unknown_accounts_with_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v2.sqlite"
            conn = sqlite3.connect(path, isolation_level=None)
            initialize_schema(conn)
            conn.execute(
                """INSERT INTO accounts(email, imap_server, imap_port, smtp_server, smtp_port,
                                        enabled, connection_status, created_at, updated_at)
                   VALUES ('legacy@example.test', 'imap.example.test', 993, 'smtp.example.test', 465,
                           1, 'unknown', 1, 1)"""
            )
            conn.execute(
                "INSERT INTO account_secrets(account_id, nonce, ciphertext, created_at, updated_at) VALUES (1, ?, ?, 1, 1)",
                (b"nonce", b"ciphertext"),
            )
            conn.execute(
                """INSERT INTO accounts(email, imap_server, imap_port, smtp_server, smtp_port,
                                        enabled, connection_status, created_at, updated_at)
                   VALUES ('no-secret@example.test', 'imap.example.test', 993, 'smtp.example.test', 465,
                           1, 'unknown', 1, 1)"""
            )
            conn.execute(
                """INSERT INTO accounts(email, imap_server, imap_port, smtp_server, smtp_port,
                                        enabled, connection_status, created_at, updated_at)
                   VALUES ('connected@example.test', 'imap.example.test', 993, 'smtp.example.test', 465,
                           1, 'connected', 1, 1)"""
            )

            initialize_schema(conn)

            self.assertEqual(
                (0, "checking", None, 0),
                conn.execute(
                    "SELECT enabled, connection_status, connection_error, verification_attempts FROM accounts WHERE id = 1"
                ).fetchone(),
            )
            self.assertIsNotNone(conn.execute("SELECT next_verification_at FROM accounts WHERE id = 1").fetchone()[0])
            self.assertEqual(
                (1, "unknown"),
                conn.execute("SELECT enabled, connection_status FROM accounts WHERE id = 2").fetchone(),
            )
            self.assertEqual(
                (1, "connected"),
                conn.execute("SELECT enabled, connection_status FROM accounts WHERE id = 3").fetchone(),
            )
            # The backfill is idempotent: once queued, later upgrades do not
            # reset the worker's retry state.
            conn.execute("UPDATE accounts SET verification_attempts = 3 WHERE id = 1")
            initialize_schema(conn)
            self.assertEqual(3, conn.execute("SELECT verification_attempts FROM accounts WHERE id = 1").fetchone()[0])
            conn.close()

    def test_validation_rejects_foreign_key_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, key = root / "v2.sqlite", root / "key"
            key.write_bytes(b"q" * 32)
            previous = os.environ.get("MASTER_KEY_FILE")
            os.environ["MASTER_KEY_FILE"] = str(key)
            try:
                initialize_empty_v2(target)
            finally:
                if previous is None:
                    os.environ.pop("MASTER_KEY_FILE", None)
                else:
                    os.environ["MASTER_KEY_FILE"] = previous
            conn = sqlite3.connect(target)
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("INSERT INTO emails(account_id, mailbox, uid, uidvalidity, direction, created_at, updated_at) VALUES (999, 'INBOX', 'broken', '', 'incoming', 1, 1)")
            conn.commit()
            conn.close()
            with self.assertRaises(ValueError):
                validate_v2(target)
