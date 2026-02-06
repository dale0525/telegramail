from __future__ import annotations

from typing import Any, Optional

from aiotdlib.api import (
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    ReplyMarkupInlineKeyboard,
)

from app.database import DBManager
from app.email_utils.account_manager import AccountManager
from app.email_utils.labels import LLM_EMAIL_CATEGORIES, normalize_llm_category
from app.i18n import _

LABEL_DEFAULT_DAYS = 7
LABEL_MAX_DAYS = 90
LABEL_PAGE_SIZE = 5


def resolve_chat_scope_account_ids(chat_id: int) -> list[int]:
    account_manager = AccountManager()
    ids: list[int] = []
    seen: set[int] = set()
    for account in account_manager.get_all_accounts():
        try:
            group_id = int(account.get("tg_group_id") or 0)
            account_id = int(account.get("id"))
        except Exception:
            continue
        if group_id != int(chat_id):
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
    return ids


def parse_days(raw: Optional[str], default: int = LABEL_DEFAULT_DAYS) -> int:
    if raw is None:
        return int(default)
    text = str(raw).strip().lower()
    if text.endswith("days"):
        text = text[:-4].strip()
    elif text.endswith("day"):
        text = text[:-3].strip()
    elif text.endswith("d"):
        text = text[:-1].strip()
    elif text.endswith("天"):
        text = text[:-1].strip()
    elif text.endswith("日"):
        text = text[:-1].strip()
    try:
        days = int(text)
    except Exception:
        days = int(default)
    if days < 1:
        days = 1
    if days > LABEL_MAX_DAYS:
        days = LABEL_MAX_DAYS
    return days


def _build_panel_rows(*, days: int) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for category in LLM_EMAIL_CATEGORIES:
        button = InlineKeyboardButton(
            text=_(f"email_category_{category}"),
            type=InlineKeyboardButtonTypeCallback(
                data=f"label:list:{category}:{int(days)}:0".encode("utf-8")
            ),
        )
        pair.append(button)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    rows.append(
        [
            InlineKeyboardButton(
                text=f"📊 {_('label_stats_7d')}",
                type=InlineKeyboardButtonTypeCallback(data=b"label:stats:7"),
            ),
            InlineKeyboardButton(
                text=f"📊 {_('label_stats_30d')}",
                type=InlineKeyboardButtonTypeCallback(data=b"label:stats:30"),
            ),
        ]
    )
    return rows


def build_label_panel(*, days: int = LABEL_DEFAULT_DAYS) -> tuple[str, ReplyMarkupInlineKeyboard]:
    days_int = parse_days(str(days), default=LABEL_DEFAULT_DAYS)
    text = (
        f"🏷️ {_('label_panel_title')}\n\n"
        f"{_('label_panel_hint', days=days_int)}\n"
        "/label task 7d · /label stats 30d"
    )
    return text, ReplyMarkupInlineKeyboard(rows=_build_panel_rows(days=days_int))


def build_label_list_view(
    *,
    category: str,
    days: int,
    offset: int,
    account_ids: list[int],
) -> tuple[str, ReplyMarkupInlineKeyboard]:
    db = DBManager()
    category_norm = normalize_llm_category(category)
    days_int = parse_days(str(days), default=LABEL_DEFAULT_DAYS)
    offset_int = max(0, int(offset))
    rows = db.list_labeled_emails(
        category=category_norm,
        days=days_int,
        account_ids=account_ids,
        limit=LABEL_PAGE_SIZE,
        offset=offset_int,
    )
    total = db.count_labeled_emails(
        category=category_norm,
        days=days_int,
        account_ids=account_ids,
    )

    title = _(
        "label_list_title",
        category=_(f"email_category_{category_norm}"),
        days=days_int,
        total=total,
    )
    lines = [f"🏷️ {title}"]

    kb_rows: list[list[InlineKeyboardButton]] = []
    if rows:
        for idx, item in enumerate(rows, start=offset_int + 1):
            subject = str(item.get("subject") or _("no_subject")).strip()
            sender = str(item.get("sender") or "").strip()
            email_date = str(item.get("email_date") or "").strip()
            mailbox = str(item.get("mailbox") or "").strip()
            priority_raw = str(item.get("llm_priority") or "medium").strip().lower()
            lines.append(f"\n{idx}. {subject}")
            if sender:
                lines.append(f"   👤 {sender}")

            meta: list[str] = []
            if priority_raw:
                meta.append(_(f"email_priority_{priority_raw}"))
            if mailbox:
                meta.append(f"📁 {mailbox}")
            if email_date:
                meta.append(f"🕒 {email_date}")
            if meta:
                lines.append(f"   {' | '.join(meta)}")

            thread_id_raw = str(item.get("telegram_thread_id") or "").strip()
            try:
                thread_id = int(thread_id_raw)
                email_id = int(item.get("id"))
            except Exception:
                continue

            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"↩️ #{idx}",
                        type=InlineKeyboardButtonTypeCallback(
                            data=f"email:reply:{email_id}:{thread_id}".encode("utf-8")
                        ),
                    ),
                    InlineKeyboardButton(
                        text=f"➡️ #{idx}",
                        type=InlineKeyboardButtonTypeCallback(
                            data=f"email:forward:{email_id}:{thread_id}".encode("utf-8")
                        ),
                    ),
                    InlineKeyboardButton(
                        text=f"📌 #{idx}",
                        type=InlineKeyboardButtonTypeCallback(
                            data=f"label:locate:{thread_id}".encode("utf-8")
                        ),
                    ),
                ]
            )
    else:
        lines.append(f"\nℹ️ {_('label_no_results')}")

    nav_row: list[InlineKeyboardButton] = []
    if offset_int > 0:
        prev_offset = max(0, offset_int - LABEL_PAGE_SIZE)
        nav_row.append(
            InlineKeyboardButton(
                text=f"◀️ {_('label_prev_page')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"label:list:{category_norm}:{days_int}:{prev_offset}".encode(
                        "utf-8"
                    )
                ),
            )
        )
    if offset_int + LABEL_PAGE_SIZE < total:
        next_offset = offset_int + LABEL_PAGE_SIZE
        nav_row.append(
            InlineKeyboardButton(
                text=f"{_('label_next_page')} ▶️",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"label:list:{category_norm}:{days_int}:{next_offset}".encode(
                        "utf-8"
                    )
                ),
            )
        )
    if nav_row:
        kb_rows.append(nav_row)

    kb_rows.append(
        [
            InlineKeyboardButton(
                text=f"📊 {_('label_stats_7d')}",
                type=InlineKeyboardButtonTypeCallback(data=b"label:stats:7"),
            ),
            InlineKeyboardButton(
                text=f"🏷️ {_('label_back_panel')}",
                type=InlineKeyboardButtonTypeCallback(data=b"label:panel"),
            ),
        ]
    )

    return "\n".join(lines).strip(), ReplyMarkupInlineKeyboard(rows=kb_rows)


def build_label_stats_view(
    *,
    days: int,
    account_ids: list[int],
) -> tuple[str, ReplyMarkupInlineKeyboard]:
    db = DBManager()
    days_int = parse_days(str(days), default=LABEL_DEFAULT_DAYS)
    stats = db.count_labeled_emails_by_category(days=days_int, account_ids=account_ids)

    lines = [f"📊 {_('label_stats_title', days=days_int)}"]
    total = 0
    for category in LLM_EMAIL_CATEGORIES:
        count = int(stats.get(category, 0))
        total += count
        lines.append(f"- {_(f'email_category_{category}')}: {count}")
    lines.append(f"\n{_('label_stats_total', total=total)}")

    kb_rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for category in LLM_EMAIL_CATEGORIES:
        pair.append(
            InlineKeyboardButton(
                text=_(f"email_category_{category}"),
                type=InlineKeyboardButtonTypeCallback(
                    data=f"label:list:{category}:{days_int}:0".encode("utf-8")
                ),
            )
        )
        if len(pair) == 2:
            kb_rows.append(pair)
            pair = []
    if pair:
        kb_rows.append(pair)

    kb_rows.append(
        [
            InlineKeyboardButton(
                text=f"🏷️ {_('label_back_panel')}",
                type=InlineKeyboardButtonTypeCallback(data=b"label:panel"),
            )
        ]
    )

    return "\n".join(lines), ReplyMarkupInlineKeyboard(rows=kb_rows)


def parse_label_list_callback(data: str) -> Optional[dict[str, Any]]:
    # label:list:<category>:<days>:<offset>
    parts = (data or "").split(":")
    if len(parts) != 5 or parts[0] != "label" or parts[1] != "list":
        return None
    category = normalize_llm_category(parts[2])
    days = parse_days(parts[3], default=LABEL_DEFAULT_DAYS)
    try:
        offset = max(0, int(parts[4]))
    except Exception:
        offset = 0
    return {"category": category, "days": days, "offset": offset}
