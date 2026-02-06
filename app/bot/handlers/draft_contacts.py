from __future__ import annotations

import hashlib
from email.utils import getaddresses
from typing import Any

from app.database import DBManager


def _normalize_email(addr: str | None) -> str:
    raw = str(addr or "").strip().lower()
    return raw


def _clean_display_name(name: str | None) -> str:
    value = str(name or "").replace("\r", " ").replace("\n", " ").strip()
    return " ".join(value.split())


def _iter_parsed_addresses(raw_value: str | None) -> list[tuple[str, str]]:
    try:
        return list(getaddresses([raw_value or ""]))
    except Exception:
        return []


def list_draft_contacts(
    *,
    db: DBManager,
    account_id: int,
    query: str = "",
    limit: int = 20,
) -> list[dict[str, str]]:
    """
    Build recent contact candidates for an account from email history.

    Contacts are extracted from sender/recipient/cc/bcc fields of both incoming and
    outgoing messages, excluding the account's own identity addresses.
    """
    account = db.get_account(id=int(account_id)) or {}
    own_addrs = {_normalize_email(account.get("email"))}
    for ident in db.list_account_identities(account_id=int(account_id)):
        own_addrs.add(_normalize_email(ident.get("from_email")))
    own_addrs.discard("")

    conn = db._get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, sender, recipient, cc, bcc
        FROM emails
        WHERE email_account = ?
        ORDER BY id DESC
        LIMIT 800
        """,
        (int(account_id),),
    )
    rows = cursor.fetchall()
    conn.close()

    contacts: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row:
            continue
        email_row_id = int(row[0] or 0)
        for raw in row[1:]:
            for name, addr in _iter_parsed_addresses(raw):
                email_addr = _normalize_email(addr)
                if not email_addr or "@" not in email_addr:
                    continue
                if email_addr in own_addrs:
                    continue

                display_name = _clean_display_name(name)
                existing = contacts.get(email_addr)
                if not existing:
                    contacts[email_addr] = {
                        "email": email_addr,
                        "display_name": display_name,
                        "last_seen_id": email_row_id,
                    }
                    continue

                if email_row_id > int(existing.get("last_seen_id") or 0):
                    existing["last_seen_id"] = email_row_id
                if not existing.get("display_name") and display_name:
                    existing["display_name"] = display_name

    query_lower = str(query or "").strip().lower()
    items = list(contacts.values())
    if query_lower:
        items = [
            item
            for item in items
            if (query_lower in str(item.get("email") or "").lower())
            or (query_lower in str(item.get("display_name") or "").lower())
        ]

    items.sort(key=lambda item: (-int(item.get("last_seen_id") or 0), item["email"]))
    bounded_limit = max(1, min(int(limit or 20), 2000))
    return [
        {
            "email": str(item.get("email") or ""),
            "display_name": str(item.get("display_name") or ""),
        }
        for item in items[:bounded_limit]
        if item.get("email")
    ]


def make_contact_token(*, field: str, email_addr: str) -> str:
    raw = f"{(field or '').strip().lower()}:{_normalize_email(email_addr)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def resolve_contact_email_by_token(
    *,
    field: str,
    contacts: list[dict[str, str]],
    token: str,
) -> str:
    wanted = (token or "").strip().lower()
    if not wanted:
        return ""
    for contact in contacts:
        email_addr = _normalize_email(contact.get("email"))
        if not email_addr:
            continue
        if make_contact_token(field=field, email_addr=email_addr) == wanted:
            return email_addr
    return ""


def format_contact_button_label(*, display_name: str, email_addr: str) -> str:
    name = _clean_display_name(display_name)
    email_value = _normalize_email(email_addr)
    if name and name.lower() not in email_value:
        return f"{name} <{email_value}>"
    return email_value


def append_contact_email(*, existing_addrs: str | None, email_addr: str) -> str:
    new_email = _normalize_email(email_addr)
    if not new_email:
        return str(existing_addrs or "").strip()

    current = str(existing_addrs or "").strip()
    parsed = []
    seen = set()
    for _name, addr in _iter_parsed_addresses(current):
        normalized = _normalize_email(addr)
        if not normalized:
            continue
        if normalized in seen:
            continue
        parsed.append(normalized)
        seen.add(normalized)

    if parsed:
        if new_email not in seen:
            parsed.append(new_email)
        return ", ".join(parsed)

    if not current:
        return new_email

    plain_parts = [p.strip() for p in current.split(",") if p.strip()]
    lowered = {p.lower() for p in plain_parts}
    if new_email.lower() not in lowered:
        plain_parts.append(new_email)
    return ", ".join(plain_parts)
