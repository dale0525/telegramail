"""Versioned SQLite schema used by the v2 API and workers."""

import sqlite3
import time


CURRENT_SCHEMA_VERSION = "2"
REQUIRED_TABLES = (
    "schema_migrations", "admin_binding", "accounts", "account_secrets", "account_identities",
    "account_delete_operations",
    "mail_threads", "emails", "email_inline_assets", "email_attachments", "contacts", "drafts", "draft_recipients", "draft_attachments",
    "send_operations", "delete_operations", "delete_operation_targets", "ingestion_leases", "imap_cursors", "bot_updates", "telegram_delivery_parts",
    "llm_settings", "summary_tasks", "emails_fts",
)
REQUIRED_INDICES = (
    "one_default_identity_per_account", "mail_threads_telegram_topic", "emails_account_message_id",
    "emails_thread_id", "emails_imap_epoch_uid", "emails_llm_category_labeled_at", "email_inline_assets_email_id", "email_attachments_email_id", "contacts_lookup", "drafts_active", "send_operations_work", "delete_operations_work", "delete_operation_targets_operation",
    "summary_tasks_work", "account_delete_operations_work", "mail_threads_status_latest_at",
    "emails_thread_tombstone_created", "delete_operations_thread_status",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_binding (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    telegram_user_id INTEGER NOT NULL UNIQUE,
    private_chat_id INTEGER,
    inbox_panel_chat_id INTEGER,
    inbox_panel_message_id INTEGER,
    inbox_panel_cursor TEXT,
    inbox_panel_search TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    alias TEXT NOT NULL DEFAULT '',
    imap_server TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    imap_ssl INTEGER NOT NULL DEFAULT 1 CHECK (imap_ssl IN (0, 1)),
    smtp_server TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    smtp_ssl INTEGER NOT NULL DEFAULT 1 CHECK (smtp_ssl IN (0, 1)),
    imap_monitored_mailboxes TEXT,
    signature TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    deleted_at INTEGER,
    connection_status TEXT NOT NULL DEFAULT 'unknown',
    connection_error TEXT,
    last_verified_at INTEGER,
    next_verification_at INTEGER,
    verification_attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (email, smtp_server)
);

CREATE TABLE IF NOT EXISTS account_secrets (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_delete_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'deleting', 'deleted', 'failed')),
    purge_data INTEGER NOT NULL DEFAULT 1 CHECK (purge_data IN (0, 1)),
    lease_token TEXT,
    lease_until INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS account_delete_operations_work
    ON account_delete_operations(status, lease_until, updated_at);

CREATE TABLE IF NOT EXISTS account_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    from_email TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    reply_to TEXT,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (account_id, from_email)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_default_identity_per_account
    ON account_identities(account_id) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS mail_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    subject_normalized TEXT,
    root_message_id TEXT,
    telegram_chat_id INTEGER,
    telegram_message_thread_id INTEGER,
    latest_email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    latest_at INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(account_id, root_message_id)
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    thread_id INTEGER REFERENCES mail_threads(id) ON DELETE SET NULL,
    message_id TEXT,
    mailbox TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT,
    uidvalidity TEXT NOT NULL DEFAULT '',
    sender TEXT,
    recipient TEXT,
    cc TEXT,
    bcc TEXT,
    subject TEXT,
    email_date TEXT,
    body_text TEXT,
    body_html TEXT,
    delivered_to TEXT,
    in_reply_to TEXT,
    references_header TEXT,
    llm_category TEXT,
    llm_priority TEXT,
    llm_confidence REAL,
    llm_labeled_at INTEGER,
    llm_summary TEXT,
    llm_important_links_json TEXT NOT NULL DEFAULT '[]',
    summary_status TEXT NOT NULL DEFAULT 'pending',
    summary_updated_at INTEGER,
    direction TEXT NOT NULL DEFAULT 'incoming' CHECK (direction IN ('incoming', 'outgoing')),
    tombstoned_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(account_id, mailbox, uidvalidity, uid)
);

CREATE TABLE IF NOT EXISTS email_inline_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    content_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(email_id, content_id)
);
CREATE INDEX IF NOT EXISTS email_inline_assets_email_id ON email_inline_assets(email_id, id);

-- Non-inline MIME parts are retained on disk so a Telegram Topic can expose
-- them without re-reading an eventually unavailable mailbox.  Files are
-- addressed by email id and attachment position; the repository validates
-- that every path remains below the installation data directory before use.
CREATE TABLE IF NOT EXISTS email_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL DEFAULT 0,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(email_id, part_index)
);
CREATE INDEX IF NOT EXISTS email_attachments_email_id ON email_attachments(email_id, id);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    frequency INTEGER NOT NULL DEFAULT 0,
    last_seen_at INTEGER,
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(account_id, email)
);
CREATE INDEX IF NOT EXISTS contacts_lookup ON contacts(account_id, email COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    thread_id INTEGER REFERENCES mail_threads(id) ON DELETE SET NULL,
    draft_type TEXT NOT NULL DEFAULT 'compose',
    from_identity_email TEXT,
    subject TEXT,
    in_reply_to TEXT,
    references_header TEXT,
    body_markdown TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'archived', 'sent', 'discarded')),
    migration_hold INTEGER NOT NULL DEFAULT 0 CHECK (migration_hold IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_active ON drafts(account_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS draft_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('to', 'cc', 'bcc')),
    email TEXT NOT NULL,
    display_name TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(draft_id, recipient_type, email)
);

CREATE TABLE IF NOT EXISTS draft_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    file_id INTEGER,
    remote_id TEXT,
    file_type TEXT,
    file_name TEXT NOT NULL,
    mime_type TEXT,
    size INTEGER,
    availability TEXT NOT NULL DEFAULT 'available' CHECK (availability IN ('available', 'legacy_missing')),
    local_path TEXT,
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'legacy_missing')),
    sha256 TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS send_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    draft_id INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'ambiguous')),
    provider_message_id TEXT,
    error_code TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    UNIQUE(account_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS send_operations_work ON send_operations(status, updated_at);

CREATE TABLE IF NOT EXISTS delete_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    thread_id INTEGER REFERENCES mail_threads(id) ON DELETE SET NULL,
    provider_mailbox TEXT,
    provider_uid TEXT,
    provider_uidvalidity TEXT,
    telegram_chat_id INTEGER,
    telegram_message_thread_id INTEGER,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'deleting', 'deleted', 'failed')),
    error_code TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until INTEGER,
    provider_deleted INTEGER NOT NULL DEFAULT 0 CHECK (provider_deleted IN (0, 1)),
    topic_delete_requested INTEGER NOT NULL DEFAULT 0 CHECK (topic_delete_requested IN (0, 1)),
    topic_deleted INTEGER NOT NULL DEFAULT 0 CHECK (topic_deleted IN (0, 1)),
    tombstoned INTEGER NOT NULL DEFAULT 0 CHECK (tombstoned IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    UNIQUE(account_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS delete_operations_work ON delete_operations(status, updated_at);

CREATE TABLE IF NOT EXISTS delete_operation_targets (
    delete_operation_id INTEGER NOT NULL REFERENCES delete_operations(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    provider_mailbox TEXT,
    provider_uid TEXT,
    provider_uidvalidity TEXT,
    provider_deleted INTEGER NOT NULL DEFAULT 0 CHECK (provider_deleted IN (0, 1)),
    provider_attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(delete_operation_id, email_id)
);
CREATE INDEX IF NOT EXISTS delete_operation_targets_operation ON delete_operation_targets(delete_operation_id, email_id);

CREATE TABLE IF NOT EXISTS ingestion_leases (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    owner_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS imap_cursors (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    mailbox TEXT NOT NULL,
    uidvalidity TEXT,
    last_uid INTEGER NOT NULL DEFAULT 0 CHECK (last_uid >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, mailbox)
);

CREATE TABLE IF NOT EXISTS bot_updates (
    update_id INTEGER PRIMARY KEY,
    update_type TEXT,
    payload_json TEXT,
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'processing', 'processed', 'failed')),
    received_at INTEGER NOT NULL,
    processed_at INTEGER,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS telegram_delivery_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL,
    telegram_chat_id INTEGER,
    telegram_message_id INTEGER,
    message_kind TEXT,
    status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'queued', 'delivering', 'ambiguous', 'delivered', 'failed')),
    lease_token TEXT,
    lease_until INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    topic_delete_markup_version INTEGER NOT NULL DEFAULT 0,
    topic_delete_markup_attempts INTEGER NOT NULL DEFAULT 0,
    topic_action_markup_version INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(email_id, part_index)
);

-- There is exactly one LLM configuration for the installation.  The API key
-- is intentionally split into nonce/ciphertext columns and is never exposed
-- by repository/API read methods.
CREATE TABLE IF NOT EXISTS llm_settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    default_language TEXT NOT NULL DEFAULT 'en_US',
    summary_threshold INTEGER NOT NULL DEFAULT 120,
    api_key_nonce BLOB,
    api_key_ciphertext BLOB,
    last_test_status TEXT NOT NULL DEFAULT 'never',
    last_tested_at INTEGER,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Durable summary work is one task per message.  Results remain on ``emails``;
-- this table only tracks queue/lease/retry state so workers can safely claim
-- work across process restarts.
CREATE TABLE IF NOT EXISTS summary_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL UNIQUE REFERENCES emails(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'queued', 'running', 'completed', 'succeeded', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS summary_tasks_work ON summary_tasks(status, lease_until, updated_at);

"""


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create v2 tables atomically and record the current schema version."""
    now = int(time.time())
    # Table-rebuild migrations need foreign keys disabled before their transaction
    # starts.  The script opens (but deliberately does not commit) one transaction;
    # every dynamic ALTER/rebuild below therefore rolls back as a unit on failure.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        recovery_sql = _interrupted_rebuild_recovery_sql(conn)
        conn.executescript("BEGIN IMMEDIATE;\n" + recovery_sql + SCHEMA_SQL)
        _apply_schema_migrations(conn, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _interrupted_rebuild_recovery_sql(conn: sqlite3.Connection) -> str:
    """Recover temp tables left by pre-atomic v2 migrations before continuing."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    statements: list[str] = []
    for target, temporary in (
        ("emails", "emails__epoch_new"),
        ("telegram_delivery_parts", "telegram_delivery_parts__old"),
    ):
        if temporary not in tables:
            continue
        if target not in tables:
            statements.append(f"ALTER TABLE {temporary} RENAME TO {target};")
            continue
        target_count = int(conn.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0])
        temporary_count = int(conn.execute(f"SELECT COUNT(*) FROM {temporary}").fetchone()[0])
        if target_count == 0 and temporary_count > 0:
            statements.extend((f"DROP TABLE {target};", f"ALTER TABLE {temporary} RENAME TO {target};"))
        else:
            statements.append(f"DROP TABLE {temporary};")
    return "\n".join(statements) + ("\n" if statements else "")


def _apply_schema_migrations(conn: sqlite3.Connection, now: int) -> None:
    already_current = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ? LIMIT 1",
        (CURRENT_SCHEMA_VERSION,),
    ).fetchone() is not None
    existing_thread_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(mail_threads)")
    }
    projection_needs_backfill = (
        not already_current
        or "latest_email_id" not in existing_thread_columns
        or "latest_at" not in existing_thread_columns
    )
    fts_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'emails_fts'"
    ).fetchone() is not None
    search_needs_backfill = not already_current or not fts_exists

    _ensure_columns(conn, "accounts", {
        "deleted_at": "INTEGER",
        "connection_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "connection_error": "TEXT",
        "last_verified_at": "INTEGER",
        "next_verification_at": "INTEGER",
        "verification_attempts": "INTEGER NOT NULL DEFAULT 0",
    })
    # Accounts imported before background verification existed retain their
    # previous enabled flag and receive the schema default of ``unknown``.  They
    # have credentials, but are never claimed by the verification worker because
    # it only processes disabled accounts.  Queue them once during upgrade and
    # keep mail polling off until both transports have been verified.
    conn.execute(
        """UPDATE accounts
           SET enabled = 0, connection_status = 'checking', connection_error = NULL,
               last_verified_at = NULL, next_verification_at = ?,
               verification_attempts = 0, updated_at = ?
           WHERE deleted_at IS NULL AND enabled = 1 AND connection_status = 'unknown'
             AND EXISTS (SELECT 1 FROM account_secrets WHERE account_secrets.account_id = accounts.id)""",
        (now, now),
    )
    _ensure_columns(conn, "mail_threads", {
        "telegram_chat_id": "INTEGER",
        "telegram_message_thread_id": "INTEGER",
        "latest_email_id": "INTEGER",
        "latest_at": "INTEGER",
    })
    _ensure_columns(conn, "admin_binding", {
        "private_chat_id": "INTEGER",
        "inbox_panel_chat_id": "INTEGER",
        "inbox_panel_message_id": "INTEGER",
        "inbox_panel_cursor": "TEXT",
        "inbox_panel_search": "TEXT",
    })
    _ensure_columns(conn, "emails", {
        "account_id": "INTEGER",
        "mailbox": "TEXT NOT NULL DEFAULT 'INBOX'",
        "uid": "TEXT",
        "thread_id": "INTEGER",
        "message_id": "TEXT",
        "uidvalidity": "TEXT NOT NULL DEFAULT ''",
        "sender": "TEXT",
        "recipient": "TEXT",
        "cc": "TEXT",
        "bcc": "TEXT",
        "subject": "TEXT",
        "email_date": "TEXT",
        "body_text": "TEXT",
        "body_html": "TEXT",
        "delivered_to": "TEXT",
        "in_reply_to": "TEXT",
        "references_header": "TEXT",
        "tombstoned_at": "INTEGER",
        "llm_category": "TEXT",
        "llm_priority": "TEXT",
        "llm_confidence": "REAL",
        "llm_labeled_at": "INTEGER",
        "llm_summary": "TEXT",
        "llm_important_links_json": "TEXT NOT NULL DEFAULT '[]'",
        "summary_status": "TEXT NOT NULL DEFAULT 'pending'",
        "summary_updated_at": "INTEGER",
        "direction": "TEXT NOT NULL DEFAULT 'incoming'",
        "created_at": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "contacts", {
        "usage_count": "INTEGER NOT NULL DEFAULT 0",
        "last_used_at": "INTEGER",
    })
    _ensure_columns(conn, "delete_operations", {
        "thread_id": "INTEGER",
        "provider_mailbox": "TEXT",
        "provider_uid": "TEXT",
        "provider_uidvalidity": "TEXT",
        "telegram_chat_id": "INTEGER",
        "telegram_message_thread_id": "INTEGER",
        "topic_delete_requested": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "delete_operation_targets", {
        "provider_deleted": "INTEGER NOT NULL DEFAULT 0",
        "provider_uidvalidity": "TEXT",
        "provider_attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
        "updated_at": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "telegram_delivery_parts", {
        "message_kind": "TEXT",
        "lease_token": "TEXT",
        "lease_until": "INTEGER",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "topic_delete_markup_version": "INTEGER NOT NULL DEFAULT 0",
        "topic_delete_markup_attempts": "INTEGER NOT NULL DEFAULT 0",
        "topic_action_markup_version": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "llm_settings", {
        "enabled": "INTEGER NOT NULL DEFAULT 0",
        "base_url": "TEXT NOT NULL DEFAULT ''",
        "model": "TEXT NOT NULL DEFAULT ''",
        "default_language": "TEXT NOT NULL DEFAULT 'en_US'",
        "summary_threshold": "INTEGER NOT NULL DEFAULT 120",
        "api_key_nonce": "BLOB",
        "api_key_ciphertext": "BLOB",
        "last_test_status": "TEXT NOT NULL DEFAULT 'never'",
        "last_tested_at": "INTEGER",
        "failed_count": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "summary_tasks", {
        "email_id": "INTEGER",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "lease_token": "TEXT",
        "lease_until": "INTEGER",
        "last_error": "TEXT",
        "created_at": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "INTEGER NOT NULL DEFAULT 0",
        "completed_at": "INTEGER",
    })
    conn.execute(
        """INSERT OR IGNORE INTO llm_settings(singleton, enabled, base_url, model,
                  default_language, summary_threshold, last_test_status, failed_count, created_at, updated_at)
           VALUES (1, 0, '', '', 'en_US', 120, 'never', 0, ?, ?)""",
        (now, now),
    )
    # Existing v2 rows already carrying an LLM summary were completed before
    # the explicit queue status was introduced.  Preserve that state during a
    # non-rebuild migration as well as the epoch table rebuild below.
    conn.execute(
        """UPDATE emails SET summary_status = 'completed',
                  summary_updated_at = COALESCE(summary_updated_at, llm_labeled_at)
           WHERE llm_summary IS NOT NULL AND (summary_status IS NULL OR summary_status = 'pending')"""
    )
    # Keep the additive links field deterministic for rows from early
    # development databases that already had the column but allowed NULL.
    conn.execute(
        """UPDATE emails SET llm_important_links_json = '[]'
           WHERE llm_important_links_json IS NULL OR trim(llm_important_links_json) = ''"""
    )
    _ensure_columns(conn, "draft_attachments", {
        "local_path": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'available'",
        "sha256": "TEXT",
    })
    conn.execute("UPDATE draft_attachments SET status = 'legacy_missing' WHERE availability = 'legacy_missing'")
    emails_rebuilt = _upgrade_emails_epoch_if_needed(conn)
    _upgrade_delivery_parts_if_needed(conn)
    _ensure_email_search_table(conn)
    _ensure_email_search_triggers(conn)
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS mail_threads_telegram_topic
                    ON mail_threads(telegram_chat_id, telegram_message_thread_id)
                    WHERE telegram_chat_id IS NOT NULL AND telegram_message_thread_id IS NOT NULL""")
    conn.execute("CREATE INDEX IF NOT EXISTS emails_llm_category_labeled_at ON emails(llm_category, llm_labeled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS emails_account_message_id ON emails(account_id, message_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS emails_thread_id ON emails(thread_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS emails_thread_tombstone_created ON emails(thread_id, tombstoned_at, created_at DESC, id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS emails_imap_epoch_uid ON emails(account_id, mailbox, uidvalidity, uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS mail_threads_status_latest_at ON mail_threads(status, latest_at DESC, id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS delete_operations_thread_status ON delete_operations(thread_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS summary_tasks_work ON summary_tasks(status, lease_until, updated_at)")
    if projection_needs_backfill:
        _backfill_latest_email_projection(conn)
    if search_needs_backfill or emails_rebuilt:
        _backfill_email_search_index(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (CURRENT_SCHEMA_VERSION, now),
    )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _backfill_latest_email_projection(conn: sqlite3.Connection) -> None:
    """Populate the materialized latest-message pointer for existing threads."""

    conn.execute(
        """UPDATE mail_threads
           SET latest_email_id = (
                 SELECT e.id FROM emails e
                 WHERE e.thread_id = mail_threads.id AND e.tombstoned_at IS NULL
                 ORDER BY e.created_at DESC, e.id DESC LIMIT 1
           ),
               latest_at = (
                 SELECT e.created_at FROM emails e
                 WHERE e.thread_id = mail_threads.id AND e.tombstoned_at IS NULL
                 ORDER BY e.created_at DESC, e.id DESC LIMIT 1
           )"""
    )


def _backfill_email_search_index(conn: sqlite3.Connection) -> None:
    """Rebuild FTS rows idempotently after an additive schema upgrade."""

    try:
        conn.execute("DELETE FROM emails_fts")
        conn.execute(
            """INSERT INTO emails_fts(email_id, thread_id, sender, subject, summary, body)
               SELECT id, COALESCE(thread_id, 0), COALESCE(sender, ''), COALESCE(subject, ''),
                      COALESCE(llm_summary, ''), COALESCE(body_text, '') FROM emails"""
        )
    except sqlite3.OperationalError:
        # A SQLite build without FTS5 should still be able to open and upgrade
        # the mail store; repository search falls back to LIKE in that case.
        pass


def _ensure_email_search_table(conn: sqlite3.Connection) -> None:
    """Create FTS5 when available, with a LIKE-compatible fallback table."""

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'emails_fts'"
    ).fetchone()
    if exists:
        return
    try:
        conn.execute(
            """CREATE VIRTUAL TABLE emails_fts USING fts5(
                   email_id UNINDEXED, thread_id UNINDEXED,
                   sender, subject, summary, body
               )"""
        )
    except sqlite3.OperationalError:
        # Some minimal SQLite builds omit FTS5. Keep the same columns in a
        # normal table so triggers and the repository's LIKE fallback remain
        # usable; MATCH will fail over at query time.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS emails_fts(
                   email_id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL,
                   sender TEXT, subject TEXT, summary TEXT, body TEXT
               )"""
        )


def _ensure_email_search_triggers(conn: sqlite3.Connection) -> None:
    """Recreate FTS triggers after migrations that rebuild ``emails``.

    SQLite drops triggers attached to a table when that table is replaced. The
    UIDVALIDITY migration can rebuild ``emails`` after the base schema creates
    its triggers, so run these idempotently at the end of every upgrade.
    """

    try:
        # Use individual execute calls: sqlite3.executescript would implicitly
        # commit the migration transaction before creating these triggers.
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS emails_fts_ai AFTER INSERT ON emails BEGIN
                INSERT INTO emails_fts(email_id, thread_id, sender, subject, summary, body)
                VALUES (new.id, COALESCE(new.thread_id, 0), COALESCE(new.sender, ''),
                        COALESCE(new.subject, ''), COALESCE(new.llm_summary, ''), COALESCE(new.body_text, ''));
            END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS emails_fts_au AFTER UPDATE OF thread_id, sender, subject, llm_summary, body_text ON emails BEGIN
                DELETE FROM emails_fts WHERE email_id = old.id;
                INSERT INTO emails_fts(email_id, thread_id, sender, subject, summary, body)
                VALUES (new.id, COALESCE(new.thread_id, 0), COALESCE(new.sender, ''),
                        COALESCE(new.subject, ''), COALESCE(new.llm_summary, ''), COALESCE(new.body_text, ''));
            END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS emails_fts_ad AFTER DELETE ON emails BEGIN
                DELETE FROM emails_fts WHERE email_id = old.id;
            END"""
        )
    except sqlite3.OperationalError:
        # Keep the non-FTS fallback usable on SQLite builds without FTS5.
        pass


def _upgrade_emails_epoch_if_needed(conn: sqlite3.Connection) -> bool:
    """Make UIDVALIDITY part of the durable IMAP message identity without data loss."""
    expected = ["account_id", "mailbox", "uidvalidity", "uid"]
    has_epoch_unique = False
    for index in conn.execute("PRAGMA index_list(emails)"):
        if not index[2]:
            continue
        columns = [row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})")]
        if columns == expected:
            has_epoch_unique = True
            break
    if has_epoch_unique:
        return False
    now = int(time.time())
    conn.execute("DROP TABLE IF EXISTS emails__epoch_new")
    conn.execute("""INSERT OR IGNORE INTO accounts(email, alias, imap_server, imap_port, imap_ssl, smtp_server, smtp_port, smtp_ssl, enabled, created_at, updated_at)
                    VALUES ('legacy-early-orphan@invalid', 'Legacy early orphaned mail', 'legacy.invalid', 0, 0, 'legacy.invalid', 0, 0, 0, ?, ?)""",
                 (now, now))
    fallback = conn.execute("SELECT id FROM accounts WHERE email = 'legacy-early-orphan@invalid' AND smtp_server = 'legacy.invalid'").fetchone()
    fallback_id = int(fallback[0])
    # Extremely early development DBs lacked account_id.  Preserve those rows
    # under a disabled synthetic account rather than dropping them.
    conn.execute("""UPDATE emails SET account_id = ? WHERE account_id IS NULL
                    OR NOT EXISTS (SELECT 1 FROM accounts WHERE accounts.id = emails.account_id)""", (fallback_id,))
    conn.execute("""
            CREATE TABLE emails__epoch_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                thread_id INTEGER REFERENCES mail_threads(id) ON DELETE SET NULL,
                message_id TEXT,
                mailbox TEXT NOT NULL DEFAULT 'INBOX',
                uid TEXT,
                uidvalidity TEXT NOT NULL DEFAULT '',
                sender TEXT,
                recipient TEXT,
                cc TEXT,
                bcc TEXT,
                subject TEXT,
                email_date TEXT,
                body_text TEXT,
                body_html TEXT,
                delivered_to TEXT,
                in_reply_to TEXT,
                references_header TEXT,
                llm_category TEXT,
                llm_priority TEXT,
                llm_confidence REAL,
                llm_labeled_at INTEGER,
                llm_summary TEXT,
                llm_important_links_json TEXT NOT NULL DEFAULT '[]',
                summary_status TEXT NOT NULL DEFAULT 'pending',
                summary_updated_at INTEGER,
                direction TEXT NOT NULL DEFAULT 'incoming' CHECK (direction IN ('incoming', 'outgoing')),
                tombstoned_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(account_id, mailbox, uidvalidity, uid)
            )
        """)
    conn.execute("""INSERT INTO emails__epoch_new(id, account_id, thread_id, message_id, mailbox, uid, uidvalidity, sender, recipient, cc, bcc, subject, email_date, body_text, body_html, delivered_to, in_reply_to, references_header, llm_category, llm_priority, llm_confidence, llm_labeled_at, llm_summary, llm_important_links_json, summary_status, summary_updated_at, direction, tombstoned_at, created_at, updated_at)
                    SELECT id, account_id, thread_id, message_id, mailbox, uid, COALESCE(uidvalidity, ''), sender, recipient, cc, bcc, subject, email_date, body_text, body_html, delivered_to, in_reply_to, references_header, llm_category, llm_priority, llm_confidence, llm_labeled_at, llm_summary, COALESCE(llm_important_links_json, '[]'), COALESCE(summary_status, CASE WHEN llm_summary IS NOT NULL THEN 'completed' ELSE 'pending' END), summary_updated_at, direction, tombstoned_at, created_at, updated_at
                    FROM emails""")
    conn.execute("DROP TABLE emails")
    conn.execute("ALTER TABLE emails__epoch_new RENAME TO emails")
    return True


def _upgrade_delivery_parts_if_needed(conn: sqlite3.Connection) -> None:
    """Upgrade early v2 development tables that required a Telegram chat too soon."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'telegram_delivery_parts'").fetchone()
    definition = (row[0] if row else "").lower()
    if "'waiting'" in definition and "'ambiguous'" in definition and "telegram_chat_id integer not null" not in definition and "lease_token" in definition and "attempts" in definition:
        return
    conn.execute("DROP TABLE IF EXISTS telegram_delivery_parts__old")
    conn.execute("ALTER TABLE telegram_delivery_parts RENAME TO telegram_delivery_parts__old")
    conn.execute("""
        CREATE TABLE telegram_delivery_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
            part_index INTEGER NOT NULL,
            telegram_chat_id INTEGER,
            telegram_message_id INTEGER,
            message_kind TEXT,
            status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'queued', 'delivering', 'ambiguous', 'delivered', 'failed')),
            lease_token TEXT,
            lease_until INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            topic_delete_markup_version INTEGER NOT NULL DEFAULT 0,
            topic_delete_markup_attempts INTEGER NOT NULL DEFAULT 0,
            topic_action_markup_version INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(email_id, part_index)
        )
    """)
    conn.execute("""INSERT INTO telegram_delivery_parts(id, email_id, part_index, telegram_chat_id, telegram_message_id, message_kind, status, lease_token, lease_until, attempts, topic_delete_markup_version, topic_delete_markup_attempts, topic_action_markup_version, created_at, updated_at)
                    SELECT id, email_id, part_index, telegram_chat_id, telegram_message_id, NULL,
                           CASE WHEN status = 'pending' THEN 'queued' ELSE status END, NULL, NULL, 0,
                           0, 0, 0, created_at, updated_at
                    FROM telegram_delivery_parts__old""")
    conn.execute("DROP TABLE telegram_delivery_parts__old")
