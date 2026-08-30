#!/usr/bin/env python3
"""Create a new encrypted v2 SQLite database from a legacy Telegramail DB."""

from __future__ import annotations

import argparse
from datetime import timezone
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Allow direct invocation (`python scripts/migrate_v2.py`) from a checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import V2Repository
from app.db.crypto import load_master_key
from app.db.schema import REQUIRED_INDICES, REQUIRED_TABLES


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone() is not None


def _rows(conn: sqlite3.Connection, name: str) -> Iterable[sqlite3.Row]:
    if not _table_exists(conn, name):
        return []
    return conn.execute(f'SELECT * FROM "{name}"').fetchall()


def _value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() and row[name] is not None else default


def _split_addresses(value: Any) -> Iterable[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (TypeError, ValueError):
        pass
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _addresses(value: Any) -> Iterable[tuple[str, str]]:
    """Return normalized RFC address pairs without treating display text as mail."""
    if not value:
        return []
    result: list[tuple[str, str]] = []
    for display_name, address in getaddresses([str(value)]):
        normalized = address.strip().lower()
        if normalized and "@" in normalized:
            result.append((display_name.strip(), normalized))
    return result


def _legacy_timestamp(value: Any) -> Optional[int]:
    if not value:
        return None
    raw = str(value).strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    try:
        from datetime import datetime
        parsed_iso = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed_iso.tzinfo is None:
            parsed_iso = parsed_iso.replace(tzinfo=timezone.utc)
        return int(parsed_iso.timestamp())
    except ValueError:
        return None


def _legacy_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    tables = ("accounts", "account_identities", "emails", "drafts", "draft_attachments")
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) if _table_exists(conn, name) else 0 for name in tables}


def validate_v2(target: Path) -> Dict[str, Any]:
    if not target.is_file():
        raise ValueError("v2 target database does not exist")
    checked = sqlite3.connect(str(target))
    try:
        checked.row_factory = sqlite3.Row
        migration = checked.execute("SELECT version FROM schema_migrations WHERE version = '2'").fetchone()
        if migration is None:
            raise ValueError("v2 schema migration is missing")
        objects = {row["name"] for row in checked.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
        if set(REQUIRED_TABLES) - objects or set(REQUIRED_INDICES) - objects:
            raise ValueError("v2 schema is incomplete")
        integrity_row = checked.execute("PRAGMA integrity_check").fetchone()
        integrity = integrity_row[0] if integrity_row else "unknown"
        foreign = [dict(row) for row in checked.execute("PRAGMA foreign_key_check").fetchall()]
        projection_mismatches = int(checked.execute(
            """SELECT COUNT(*) FROM mail_threads t
               WHERE t.status <> 'tombstoned' AND (
                 (t.latest_email_id IS NULL AND EXISTS (
                    SELECT 1 FROM emails e WHERE e.thread_id = t.id AND e.tombstoned_at IS NULL
                 )) OR
                 (t.latest_email_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM emails e WHERE e.id = t.latest_email_id AND e.thread_id = t.id AND e.tombstoned_at IS NULL
                 )) OR
                 (t.latest_email_id IS NOT NULL AND t.latest_at <> (
                    SELECT e.created_at FROM emails e WHERE e.id = t.latest_email_id
                 )) OR
                 (t.latest_email_id IS NOT NULL AND t.latest_at IS NULL) OR
                 (t.latest_email_id IS NOT NULL AND EXISTS (
                    SELECT 1
                    FROM emails newer
                    JOIN emails latest ON latest.id = t.latest_email_id
                    WHERE newer.thread_id = t.id AND newer.tombstoned_at IS NULL
                      AND (newer.created_at > latest.created_at OR
                           (newer.created_at = latest.created_at AND newer.id > latest.id))
                 ))
               )"""
        ).fetchone()[0])
    finally:
        checked.close()
    if integrity != "ok":
        raise ValueError("v2 database integrity check failed")
    if foreign:
        raise ValueError("v2 database foreign key check failed")
    if projection_mismatches:
        raise ValueError("latest email projection is inconsistent")
    return {"target": str(target), "target_sha256": _sha256(target), "integrity_check": integrity, "foreign_key_check": foreign, "latest_projection_mismatches": projection_mismatches}


def initialize_empty_v2(target: Path) -> Dict[str, Any]:
    """Explicitly create a new v2 database; never overwrite an existing target."""
    if target.exists():
        raise ValueError("target database already exists")
    load_master_key()
    V2Repository(target)
    report = validate_v2(target)
    report["initialized"] = True
    return report


def upgrade_existing_v2(target: Path) -> Dict[str, Any]:
    """Apply the current additive v2 schema to an existing v2 database."""
    if not target.is_file():
        raise ValueError("v2 target database does not exist")
    load_master_key()
    V2Repository(target)
    report = validate_v2(target)
    report["upgraded"] = True
    return report


def migrate(source: Path, target: Path, *, dry_run: bool = False, backup: Optional[Path] = None) -> Dict[str, Any]:
    if not source.is_file():
        raise ValueError("legacy source database does not exist")
    if source.resolve() == target.resolve():
        raise ValueError("target must be a new database file")
    if target.exists():
        raise ValueError("target database already exists")

    legacy = sqlite3.connect(str(source))
    legacy.row_factory = sqlite3.Row
    try:
        source_integrity_row = legacy.execute("PRAGMA integrity_check").fetchone()
        source_integrity = source_integrity_row[0] if source_integrity_row else "unknown"
        source_foreign = [dict(row) for row in legacy.execute("PRAGMA foreign_key_check").fetchall()]
        if source_integrity != "ok":
            raise ValueError("legacy database integrity check failed")
        if source_foreign:
            raise ValueError("legacy database foreign key check failed")
        report: Dict[str, Any] = {"dry_run": dry_run, "source": str(source), "target": str(target), "source_sha256": _sha256(source), "source_counts": _legacy_counts(legacy), "source_integrity_check": source_integrity, "source_foreign_key_check": source_foreign}
        if dry_run:
            report.update({"planned_counts": report["source_counts"], "backup": None, "target_sha256": None, "foreign_key_check": [], "integrity_check": "not-run"})
            return report

        backup_path = backup or source.with_suffix(source.suffix + ".pre-v2-backup")
        if backup_path.exists():
            raise ValueError("backup file already exists")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_path)
        repo = V2Repository(target)
        account_map: Dict[int, int] = {}
        counts = {"accounts": 0, "account_identities": 0, "emails": 0, "llm_labeled_emails": 0, "contacts": 0, "drafts": 0, "draft_attachments": 0}
        skipped = {"emails": 0}
        conflicts = {"emails": 0}
        own_addresses: Dict[int, set[str]] = {}

        for old in _rows(legacy, "accounts"):
            password = _value(old, "password", "")
            created = repo.create_account({
                "email": _value(old, "email", ""), "alias": _value(old, "alias", ""),
                "imap_server": _value(old, "imap_server", ""), "imap_port": _value(old, "imap_port", 0), "imap_ssl": bool(_value(old, "imap_ssl", 1)),
                "smtp_server": _value(old, "smtp_server", ""), "smtp_port": _value(old, "smtp_port", 0), "smtp_ssl": bool(_value(old, "smtp_ssl", 1)),
                "imap_monitored_mailboxes": _value(old, "imap_monitored_mailboxes"), "signature": _value(old, "signature"),
            }, str(password))
            account_map[int(old["id"])] = int(created["id"])
            own_addresses[int(created["id"])] = {str(created["email"]).strip().lower()}
            counts["accounts"] += 1

        # Historical databases can contain messages after an account was
        # removed.  Preserve them under a clearly disabled synthetic account;
        # no address or message content is reported by this migration.
        orphan_legacy_ids = {
            int(_value(old, "email_account", -1))
            for old in _rows(legacy, "emails")
            if int(_value(old, "email_account", -1)) not in account_map
        }
        orphan_email_count = sum(
            1 for old in _rows(legacy, "emails")
            if int(_value(old, "email_account", -1)) in orphan_legacy_ids
        )
        if orphan_legacy_ids:
            orphan = repo.create_account({
                "email": "legacy-orphan@invalid", "alias": "Legacy orphaned mail", "imap_server": "legacy.invalid", "imap_port": 0,
                "imap_ssl": False, "smtp_server": "legacy.invalid", "smtp_port": 0, "smtp_ssl": False, "enabled": False,
            }, "")
            for legacy_id in orphan_legacy_ids:
                account_map[legacy_id] = int(orphan["id"])
            counts["accounts"] += 1

        for old in _rows(legacy, "account_identities"):
            account_id = account_map.get(int(_value(old, "account_id", -1)))
            if account_id is None:
                continue
            repo.upsert_identity(account_id, str(_value(old, "from_email", "")), str(_value(old, "display_name", "")),
                                 reply_to=_value(old, "reply_to"), is_default=bool(_value(old, "is_default", 0)), enabled=bool(_value(old, "enabled", 1)))
            own_addresses.setdefault(account_id, set()).add(str(_value(old, "from_email", "")).strip().lower())
            counts["account_identities"] += 1

        # Email history is copied without legacy Telegram topic/group/cursor links.
        for old in _rows(legacy, "emails"):
            account_id = account_map.get(int(_value(old, "email_account", -1)))
            if account_id is None:
                continue
            now = int(time.time())
            llm_category = _value(old, "llm_category")
            llm_priority = _value(old, "llm_priority", _value(old, "priority"))
            llm_confidence = _value(old, "llm_confidence", _value(old, "confidence"))
            llm_labeled_at = _value(old, "llm_labeled_at", _value(old, "labeled_at"))
            llm_summary = _value(old, "llm_summary", _value(old, "summary"))
            with repo.db.transaction(immediate=True) as conn:
                cur = conn.execute("""INSERT OR IGNORE INTO emails(account_id, message_id, mailbox, uid, sender, recipient, cc, bcc, subject, email_date, body_text, body_html, delivered_to, in_reply_to, references_header, llm_category, llm_priority, llm_confidence, llm_labeled_at, llm_summary, direction, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (account_id, _value(old, "message_id"), _value(old, "mailbox", "INBOX"), _value(old, "uid"), _value(old, "sender"), _value(old, "recipient"), _value(old, "cc"), _value(old, "bcc"), _value(old, "subject"), _value(old, "email_date"), _value(old, "body_text"), _value(old, "body_html"), _value(old, "delivered_to"), _value(old, "in_reply_to"), _value(old, "references_header"), llm_category, llm_priority, llm_confidence, llm_labeled_at, llm_summary, "outgoing" if str(_value(old, "mailbox", "")).upper() == "OUTGOING" else "incoming", now, now))
            if cur.rowcount:
                if llm_labeled_at is not None or llm_category is not None:
                    counts["llm_labeled_emails"] += 1
                counts["emails"] += 1
            else:
                skipped["emails"] += 1
                conflicts["emails"] += 1

        # Build contacts from migrated address history.  Own account/identity
        # addresses are excluded; duplicate normalized addresses aggregate
        # usage_count/frequency and retain the newest parseable mail timestamp.
        for old in _rows(legacy, "emails"):
            account_id = account_map.get(int(_value(old, "email_account", -1)))
            if account_id is None:
                continue
            seen_at = _legacy_timestamp(_value(old, "email_date"))
            for column in ("sender", "recipient", "cc", "bcc", "delivered_to"):
                for display_name, address in _addresses(_value(old, column)):
                    if address in own_addresses.get(account_id, set()):
                        continue
                    repo.record_contact_history(account_id, address, display_name, used_at=seen_at)
        conn = repo.db.connect()
        try:
            counts["contacts"] = int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
        finally:
            conn.close()

        draft_map: Dict[int, int] = {}
        for old in _rows(legacy, "drafts"):
            account_id = account_map.get(int(_value(old, "account_id", -1)))
            if account_id is None:
                continue
            status = "archived" if _value(old, "status", "open") == "open" else _value(old, "status", "archived")
            if status not in {"archived", "sent", "discarded"}:
                status = "archived"
            draft = repo.create_draft(account_id, draft_type=str(_value(old, "draft_type", "compose")), from_identity_email=_value(old, "from_identity_email"),
                                      subject=_value(old, "subject"), body_markdown=_value(old, "body_markdown"), status=status, migration_hold=True)
            draft_map[int(old["id"])] = int(draft["id"])
            recipients: List[Dict[str, Any]] = []
            for legacy_field, kind in (("to_addrs", "to"), ("cc_addrs", "cc"), ("bcc_addrs", "bcc")):
                recipients.extend({"type": kind, "email": address} for address in _split_addresses(_value(old, legacy_field)))
            repo.replace_draft_recipients(int(draft["id"]), recipients)
            counts["drafts"] += 1

        for old in _rows(legacy, "draft_attachments"):
            draft_id = draft_map.get(int(_value(old, "draft_id", -1)))
            if draft_id is None:
                continue
            repo.add_draft_attachment(draft_id, file_name=str(_value(old, "file_name", "attachment")), file_id=_value(old, "file_id"), remote_id=_value(old, "remote_id"),
                                      file_type=_value(old, "file_type"), mime_type=_value(old, "mime_type"), size=_value(old, "size"), availability="legacy_missing")
            counts["draft_attachments"] += 1

        target_validation = validate_v2(target)
        report.update({"backup": str(backup_path), "migrated_counts": counts, "skipped": skipped, "conflicts": conflicts,
                       "orphan_emails_migrated": orphan_email_count, **target_validation})
        return report
    finally:
        legacy.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    data_dir = Path(__import__("os").environ.get("TELEGRAMAIL_DATA_DIR", "data"))
    parser.add_argument("--source", type=Path, default=data_dir / "telegramail.db")
    parser.add_argument("--target", type=Path, default=data_dir / "telegramail-v2.db")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--init", action="store_true", help="explicitly initialize a new empty v2 target database")
    parser.add_argument("--upgrade", action="store_true", help="apply the current schema to an existing v2 target database")
    parser.add_argument("--check-only", action="store_true", help="validate an existing v2 database and master key without writes")
    args = parser.parse_args(argv)
    try:
        if args.init:
            if args.dry_run or args.backup is not None or args.check_only or args.upgrade:
                raise ValueError("--init cannot be combined with migration options")
            load_master_key()
            report = initialize_empty_v2(args.target)
        elif args.upgrade:
            if args.dry_run or args.backup is not None or args.check_only:
                raise ValueError("--upgrade cannot be combined with migration options")
            report = upgrade_existing_v2(args.target)
        elif args.check_only:
            if args.dry_run or args.backup is not None:
                raise ValueError("--check-only cannot be combined with migration options")
            load_master_key()
            report = validate_v2(args.target)
            report["check_only"] = True
        else:
            report = migrate(args.source, args.target, dry_run=args.dry_run, backup=args.backup)
    except Exception as exc:
        # Never include database rows or encryption material in migration errors.
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
