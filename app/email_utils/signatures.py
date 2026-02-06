import json
import secrets
import time
from typing import Any

from app.database import DBManager

CHOICE_DEFAULT = "__default__"
CHOICE_NONE = "__none__"
_LEGACY_ID = "legacy"
_DEFAULT_NAME = "Default"


def _to_text(value: Any) -> str:
    return str(value or "").strip()


def _new_signature_id(existing_ids: set[str]) -> str:
    while True:
        candidate = secrets.token_hex(4)
        if candidate not in existing_ids:
            return candidate


def _normalize_items(raw_items: list[Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        markdown = _to_text(raw.get("markdown"))
        if not markdown:
            continue

        item_id = _to_text(raw.get("id"))
        if not item_id:
            item_id = _new_signature_id(seen_ids)
        if item_id in seen_ids:
            item_id = _new_signature_id(seen_ids)

        name = _to_text(raw.get("name")) or _DEFAULT_NAME
        seen_ids.add(item_id)
        items.append(
            {
                "id": item_id,
                "name": name,
                "markdown": markdown,
            }
        )

    return items


def _ensure_signature_state_tables(db: DBManager) -> None:
    conn = db._get_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS draft_signature_choices (
            draft_id INTEGER PRIMARY KEY,
            choice TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS account_signature_prefs (
            account_id INTEGER PRIMARY KEY,
            last_choice TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def list_account_signatures(raw: str | None) -> tuple[list[dict[str, str]], str | None]:
    text = _to_text(raw)
    if not text:
        return [], None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            items = _normalize_items(list(parsed.get("items") or []))
            if not items:
                return [], None
            default_id = _to_text(parsed.get("default"))
            ids = {it["id"] for it in items}
            if default_id not in ids:
                default_id = items[0]["id"]
            return items, default_id
    except Exception:
        pass

    # Backward compatibility: plain-text signature stored directly.
    return (
        [
            {
                "id": _LEGACY_ID,
                "name": _DEFAULT_NAME,
                "markdown": text,
            }
        ],
        _LEGACY_ID,
    )


def dump_account_signatures(
    items: list[dict[str, str]], default_id: str | None
) -> str | None:
    normalized = _normalize_items(items)
    if not normalized:
        return None

    ids = {it["id"] for it in normalized}
    selected_default = _to_text(default_id)
    if selected_default not in ids:
        selected_default = normalized[0]["id"]

    payload = {
        "version": 1,
        "default": selected_default,
        "items": normalized,
    }
    return json.dumps(payload, ensure_ascii=False)


def add_account_signature(
    raw: str | None, *, name: str, markdown: str
) -> tuple[str | None, str]:
    items, default_id = list_account_signatures(raw)
    existing_ids = {it["id"] for it in items}
    signature_id = _new_signature_id(existing_ids)
    items.append(
        {
            "id": signature_id,
            "name": _to_text(name) or _DEFAULT_NAME,
            "markdown": _to_text(markdown),
        }
    )
    if not default_id:
        default_id = signature_id
    return dump_account_signatures(items, default_id), signature_id


def remove_account_signature(raw: str | None, signature_id: str) -> str | None:
    items, default_id = list_account_signatures(raw)
    target = _to_text(signature_id)
    kept = [it for it in items if it.get("id") != target]
    if not kept:
        return None
    if default_id == target:
        default_id = kept[0]["id"]
    return dump_account_signatures(kept, default_id)


def set_default_account_signature(raw: str | None, signature_id: str) -> str | None:
    items, _default_id = list_account_signatures(raw)
    target = _to_text(signature_id)
    if target not in {it["id"] for it in items}:
        return dump_account_signatures(items, _default_id)
    return dump_account_signatures(items, target)


def resolve_signature_for_send(
    raw: str | None, choice: str | None
) -> tuple[str | None, str]:
    items, default_id = list_account_signatures(raw)
    if not items:
        return None, "(none)"

    pick = _to_text(choice)
    if pick == CHOICE_NONE:
        return None, "(none)"

    by_id = {it["id"]: it for it in items}
    if pick and pick not in {CHOICE_DEFAULT} and pick in by_id:
        selected = by_id[pick]
        return selected["markdown"], selected["name"]

    fallback_id = default_id if default_id in by_id else items[0]["id"]
    selected = by_id[fallback_id]
    return selected["markdown"], selected["name"]


def normalize_signature_choice(raw: str | None, choice: str | None) -> str:
    pick = _to_text(choice)
    if pick == CHOICE_NONE:
        return CHOICE_NONE

    items, _default_id = list_account_signatures(raw)
    if not items:
        return CHOICE_DEFAULT

    valid_ids = {it["id"] for it in items}
    if pick and pick not in {CHOICE_DEFAULT} and pick in valid_ids:
        return pick
    return CHOICE_DEFAULT


def resolve_signature_choice_to_store(raw: str | None, choice: str | None) -> str:
    pick = normalize_signature_choice(raw, choice)
    if pick == CHOICE_NONE:
        return CHOICE_NONE

    items, default_id = list_account_signatures(raw)
    valid_ids = {it["id"] for it in items}
    if pick not in {CHOICE_DEFAULT} and pick in valid_ids:
        return pick
    if default_id and default_id in valid_ids:
        return default_id
    return CHOICE_DEFAULT


def format_signature_choice_label(raw: str | None, choice: str | None) -> str:
    pick = _to_text(choice)
    if pick == CHOICE_NONE:
        return "None"

    items, default_id = list_account_signatures(raw)
    if not items:
        return "None"

    by_id = {it["id"]: it for it in items}
    if pick and pick not in {CHOICE_DEFAULT} and pick in by_id:
        return by_id[pick]["name"]

    fallback_id = default_id if default_id in by_id else items[0]["id"]
    return f"Default ({by_id[fallback_id]['name']})"


def set_draft_signature_choice(*, draft_id: int, choice: str | None) -> None:
    db = DBManager()
    _ensure_signature_state_tables(db)
    did = int(draft_id)
    pick = _to_text(choice)

    conn = db._get_connection()
    cur = conn.cursor()
    if not pick or pick == CHOICE_DEFAULT:
        cur.execute("DELETE FROM draft_signature_choices WHERE draft_id = ?", (did,))
    else:
        cur.execute(
            """
            INSERT INTO draft_signature_choices (draft_id, choice, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(draft_id) DO UPDATE SET
              choice = excluded.choice,
              updated_at = excluded.updated_at
            """,
            (did, pick, int(time.time())),
        )
    conn.commit()
    conn.close()


def get_draft_signature_choice(*, draft_id: int) -> str:
    db = DBManager()
    _ensure_signature_state_tables(db)
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT choice FROM draft_signature_choices WHERE draft_id = ?",
        (int(draft_id),),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return CHOICE_DEFAULT
    return _to_text(row[0]) or CHOICE_DEFAULT


def clear_draft_signature_choice(*, draft_id: int) -> None:
    db = DBManager()
    _ensure_signature_state_tables(db)
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM draft_signature_choices WHERE draft_id = ?",
        (int(draft_id),),
    )
    conn.commit()
    conn.close()


def set_account_last_signature_choice(*, account_id: int, choice: str | None) -> None:
    db = DBManager()
    _ensure_signature_state_tables(db)
    aid = int(account_id)
    pick = _to_text(choice)

    conn = db._get_connection()
    cur = conn.cursor()
    if not pick or pick == CHOICE_DEFAULT:
        cur.execute("DELETE FROM account_signature_prefs WHERE account_id = ?", (aid,))
    else:
        cur.execute(
            """
            INSERT INTO account_signature_prefs (account_id, last_choice, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
              last_choice = excluded.last_choice,
              updated_at = excluded.updated_at
            """,
            (aid, pick, int(time.time())),
        )
    conn.commit()
    conn.close()


def get_account_last_signature_choice(*, account_id: int) -> str:
    db = DBManager()
    _ensure_signature_state_tables(db)
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT last_choice FROM account_signature_prefs WHERE account_id = ?",
        (int(account_id),),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return CHOICE_DEFAULT
    return _to_text(row[0]) or CHOICE_DEFAULT
