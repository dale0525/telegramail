#!/usr/bin/env python3
"""Backfill CID image resources for already-ingested v2 messages.

The script deliberately reports only aggregate counts.  IMAP passwords are
loaded into process memory by the encrypted repository and are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import V2Repository
from app.integrations.mail.imap import IMAPTransport
from app.integrations.mail.types import IncomingMail
from app.services.v2_repository_adapter import V2MailRepositoryAdapter


def _candidate_rows(repository: V2Repository, email_id: int | None, limit: int) -> list[dict]:
    connection = repository.db.connect()
    try:
        where = [
            "e.direction = 'incoming'",
            "e.tombstoned_at IS NULL",
            "COALESCE(e.body_html, '') LIKE '%cid:%'",
        ]
        values: list[object] = []
        if email_id is not None:
            where.append("e.id = ?")
            values.append(int(email_id))
        values.append(max(1, min(int(limit), 10000)))
        return [
            dict(row)
            for row in connection.execute(
                f"""SELECT e.id, e.account_id, e.mailbox, e.uid, e.uidvalidity
                    FROM emails e
                    WHERE {' AND '.join(where)}
                    ORDER BY e.id ASC LIMIT ?""",
                tuple(values),
            ).fetchall()
        ]
    finally:
        connection.close()


async def _run(repository: V2Repository, *, email_id: int | None, limit: int) -> dict[str, int]:
    adapter = V2MailRepositoryAdapter(repository)
    rows = _candidate_rows(repository, email_id, limit)
    counts = {"candidates": len(rows), "fetched": 0, "assets": 0, "skipped": 0, "failed": 0}
    account_cache: dict[int, dict] = {}
    transport_cache: dict[int, IMAPTransport] = {}
    for row in rows:
        account_key = int(row["account_id"])
        account = account_cache.get(account_key)
        if account is None:
            account = repository.get_account(account_key)
            if not account or account.get("deleted_at") is not None:
                counts["skipped"] += 1
                continue
            try:
                account = {**account, "password": repository.get_account_password(account_key)}
            except Exception:
                counts["skipped"] += 1
                continue
            account_cache[account_key] = account
            transport_cache[account_key] = IMAPTransport(account)
        try:
            mail = await transport_cache[account_key].fetch_message(str(row["mailbox"] or "INBOX"), str(row["uid"]))
            # Reuse the stored UID epoch and exact account identity when the
            # message is inserted as an idempotent retry.
            mail = IncomingMail(
                **{
                    **mail.__dict__,
                    "uidvalidity": row["uidvalidity"] or None,
                    "email_id": int(row["id"]),
                }
            )
            counts["fetched"] += 1
            counts["assets"] += adapter.persist_inline_assets(int(row["id"]), mail.attachments)
        except Exception:
            # Continue other accounts/messages; details may contain provider
            # identifiers and are intentionally omitted from output.
            counts["failed"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    data_dir = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "data"))
    parser.add_argument("--email-id", type=int)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        repository = V2Repository(data_dir / "telegramail-v2.db")
        counts = asyncio.run(_run(repository, email_id=args.email_id, limit=args.limit))
    except Exception:
        print("inline asset backfill failed", file=sys.stderr)
        return 2
    print("inline asset backfill: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
