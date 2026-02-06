from __future__ import annotations

from email.utils import getaddresses
from typing import Any

from aiotdlib.api import InlineKeyboardButton, InlineKeyboardButtonTypeCallback

from app.bot.handlers.draft_contacts import format_contact_button_label
from app.i18n import _

_RECIPIENT_PICK_SESSIONS: dict[tuple[int, int, int, str], dict[str, Any]] = {}
_DEFAULT_PER_PAGE = 12


def _normalize_email(addr: str | None) -> str:
    return str(addr or "").strip().lower()


def _ellipsize(text: str, max_len: int) -> str:
    value = str(text or "").strip()
    if max_len <= 0:
        return ""
    if len(value) <= max_len:
        return value
    if max_len == 1:
        return "…"
    return value[: max_len - 1].rstrip() + "…"


def _session_key(*, chat_id: int, user_id: int, draft_id: int, field: str) -> tuple[int, int, int, str]:
    return int(chat_id), int(user_id), int(draft_id), str(field or "").strip().lower()


def get_recipient_target_field(field: str) -> str:
    return {
        "to": "to_addrs",
        "cc": "cc_addrs",
        "bcc": "bcc_addrs",
    }.get(str(field or "").strip().lower(), "")


def parse_recipient_addresses(raw: str | None) -> list[str]:
    value = str(raw or "").strip()
    if not value:
        return []

    parsed: list[str] = []
    seen: set[str] = set()

    try:
        parts = getaddresses([value])
    except Exception:
        parts = []

    for _name, addr in parts:
        normalized = _normalize_email(addr)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        parsed.append(normalized)

    if parsed:
        return parsed

    for part in value.split(","):
        normalized = _normalize_email(part)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        parsed.append(normalized)
    return parsed


def set_recipient_picker_session(
    *,
    chat_id: int,
    user_id: int,
    draft_id: int,
    field: str,
    session: dict[str, Any],
) -> None:
    _RECIPIENT_PICK_SESSIONS[
        _session_key(chat_id=chat_id, user_id=user_id, draft_id=draft_id, field=field)
    ] = session


def get_recipient_picker_session(
    *,
    chat_id: int,
    user_id: int,
    draft_id: int,
    field: str,
) -> dict[str, Any] | None:
    return _RECIPIENT_PICK_SESSIONS.get(
        _session_key(chat_id=chat_id, user_id=user_id, draft_id=draft_id, field=field)
    )


def clear_recipient_picker_session(
    *,
    chat_id: int,
    user_id: int,
    draft_id: int,
    field: str,
) -> None:
    _RECIPIENT_PICK_SESSIONS.pop(
        _session_key(chat_id=chat_id, user_id=user_id, draft_id=draft_id, field=field),
        None,
    )


def build_recipient_picker_session(
    *,
    draft: dict,
    field: str,
    contacts: list[dict[str, str]],
    query: str = "",
    per_page: int = _DEFAULT_PER_PAGE,
) -> dict[str, Any]:
    target_field = get_recipient_target_field(field)
    existing = parse_recipient_addresses(draft.get(target_field))
    existing_set = set(existing)

    emails: list[str] = []
    labels: list[str] = []
    seen: set[str] = set()
    for contact in contacts:
        email_addr = _normalize_email(contact.get("email"))
        if not email_addr or email_addr in seen:
            continue
        seen.add(email_addr)
        emails.append(email_addr)
        labels.append(
            format_contact_button_label(
                display_name=contact.get("display_name") or "",
                email_addr=email_addr,
            )
        )

    selected: set[int] = set()
    for idx, email_addr in enumerate(emails):
        if email_addr in existing_set:
            selected.add(idx)

    return {
        "emails": emails,
        "labels": labels,
        "selected": selected,
        "query": str(query or "").strip(),
        "page": 0,
        "per_page": max(1, int(per_page or _DEFAULT_PER_PAGE)),
    }


def build_recipient_picker_text(*, field: str, session: dict[str, Any]) -> str:
    normalized_field = str(field or "").strip().lower()
    title = {
        "to": _("draft_choose_contact_to"),
        "cc": _("draft_choose_contact_cc"),
        "bcc": _("draft_choose_contact_bcc"),
    }.get(normalized_field, _("draft_choose_contact_to"))

    emails: list[str] = list(session.get("emails") or [])
    selected: set[int] = set(session.get("selected") or set())
    selected_emails = [
        emails[idx] for idx in sorted(selected) if 0 <= int(idx) < len(emails)
    ]
    selected_display = (
        ", ".join(selected_emails)
        if selected_emails
        else _("draft_recipient_picker_none_selected")
    )
    selected_display = _ellipsize(selected_display, 1200)

    query = str(session.get("query") or "").strip()
    query_text = f"\n🔎 {query}" if query else ""

    per_page = max(1, int(session.get("per_page") or _DEFAULT_PER_PAGE))
    total_pages = max(1, (len(emails) + per_page - 1) // per_page)
    current_page = max(
        0, min(int(session.get("page") or 0), total_pages - 1)
    )
    session["page"] = current_page

    return (
        f"{title}{query_text}\n\n"
        f"{_('draft_recipient_picker_prompt')}\n\n"
        f"✅ <b>{_('draft_recipient_picker_selected')}</b>: <code>{selected_display}</code>\n"
        f"📄 {_('imap_picker_page', current=current_page + 1, total=total_pages)}"
    ).strip()


def build_recipient_picker_rows(
    *,
    draft_id: int,
    field: str,
    session: dict[str, Any],
) -> list[list[InlineKeyboardButton]]:
    emails: list[str] = list(session.get("emails") or [])
    labels: list[str] = list(session.get("labels") or [])
    selected: set[int] = set(session.get("selected") or set())
    per_page = max(1, int(session.get("per_page") or _DEFAULT_PER_PAGE))

    total_pages = max(1, (len(emails) + per_page - 1) // per_page)
    page = max(0, min(int(session.get("page") or 0), total_pages - 1))
    session["page"] = page

    start = page * per_page
    end = min(len(emails), start + per_page)

    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start, end):
        prefix = "✅" if idx in selected else "⬜️"
        label = _ellipsize(labels[idx] if idx < len(labels) else emails[idx], 40)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {label}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"draft:rcpt_pick:toggle:{int(draft_id)}:{field}:{idx}".encode(
                            "utf-8"
                        )
                    ),
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text=f"« {_('imap_picker_prev')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:rcpt_pick:page:{int(draft_id)}:{field}:{page - 1}".encode(
                        "utf-8"
                    )
                ),
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text=f"{_('imap_picker_next')} »",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:rcpt_pick:page:{int(draft_id)}:{field}:{page + 1}".encode(
                        "utf-8"
                    )
                ),
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(
                text=f"💾 {_('imap_picker_save')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:rcpt_pick:save:{int(draft_id)}:{field}".encode("utf-8")
                ),
            ),
            InlineKeyboardButton(
                text=f"✖️ {_('imap_picker_cancel')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:rcpt_pick:cancel:{int(draft_id)}:{field}".encode(
                        "utf-8"
                    )
                ),
            ),
        ]
    )
    return rows


def merge_recipient_picker_selection(
    *,
    existing_addrs: str | None,
    candidate_emails: list[str],
    selected_indices: set[int],
) -> str:
    existing = parse_recipient_addresses(existing_addrs)
    candidate = [_normalize_email(email) for email in list(candidate_emails or [])]
    candidate = [email for email in candidate if email]
    candidate_set = set(candidate)

    preserved = [email for email in existing if email not in candidate_set]
    selected = [
        candidate[idx]
        for idx in sorted(set(selected_indices or set()))
        if 0 <= int(idx) < len(candidate)
    ]

    merged: list[str] = []
    seen: set[str] = set()
    for email in preserved + selected:
        if not email or email in seen:
            continue
        seen.add(email)
        merged.append(email)
    return ", ".join(merged)
