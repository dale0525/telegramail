import html
import os

from aiotdlib import Client
from aiotdlib.api import (
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    LinkPreviewOptions,
    ReplyMarkupInlineKeyboard,
    UpdateNewCallbackQuery,
)

from app.bot.handlers.accounts import (
    add_account_handler,
    edit_account_conversation_starter,
    manual_fetch_email_handler,
)
from app.bot.utils import answer_callback
from app.email_utils.account_manager import AccountManager
from app.email_utils.imap_client import IMAPClient
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)

_MAILBOX_PICK_SESSIONS: dict[tuple[int, int, str], dict] = {}
_MAILBOX_PICK_PER_PAGE = 12


def _normalize_mailboxes_csv(raw: str) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for part in (raw or "").split(","):
        name = (part or "").strip().strip('"')
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(name)
    return ",".join(parts)


def _resolve_effective_mailboxes(account: dict) -> tuple[list[str], str]:
    account_raw = (account.get("imap_monitored_mailboxes") or "").strip()
    if account_raw:
        return IMAPClient(account)._get_monitored_mailboxes(), _(
            "imap_folders_source_account"
        )

    env_raw = (os.getenv("TELEGRAMAIL_IMAP_MONITORED_MAILBOXES") or "").strip()
    if env_raw:
        return IMAPClient(account)._get_monitored_mailboxes(), _(
            "imap_folders_source_global"
        )

    return ["INBOX"], _("imap_folders_source_default")


def _picker_key(*, chat_id: int, user_id: int, account_id: str) -> tuple[int, int, str]:
    return int(chat_id), int(user_id), str(account_id)


def _get_picker_session(*, chat_id: int, user_id: int, account_id: str) -> dict | None:
    return _MAILBOX_PICK_SESSIONS.get(_picker_key(chat_id=chat_id, user_id=user_id, account_id=account_id))


def _set_picker_session(
    *, chat_id: int, user_id: int, account_id: str, session: dict
) -> None:
    _MAILBOX_PICK_SESSIONS[_picker_key(chat_id=chat_id, user_id=user_id, account_id=account_id)] = session


def _clear_picker_session(*, chat_id: int, user_id: int, account_id: str) -> None:
    _MAILBOX_PICK_SESSIONS.pop(_picker_key(chat_id=chat_id, user_id=user_id, account_id=account_id), None)


def _ellipsize(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if max_len <= 0:
        return ""
    if len(t) <= max_len:
        return t
    if max_len == 1:
        return "…"
    return t[: max_len - 1].rstrip() + "…"


async def _render_mailbox_picker(
    *,
    client: Client,
    chat_id: int,
    message_id: int,
    user_id: int,
    account_id: str,
) -> None:
    session = _get_picker_session(chat_id=chat_id, user_id=user_id, account_id=account_id)
    if not session:
        await _render_account_mailboxes_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
            detected_preview=f"❌ {_('conversation_expired_or_not_found')}",
        )
        return

    mailboxes: list[str] = session.get("mailboxes") or []
    selected: set[int] = set(session.get("selected") or set())
    page = int(session.get("page") or 0)
    per_page = int(session.get("per_page") or _MAILBOX_PICK_PER_PAGE)

    total = max(1, (len(mailboxes) + per_page - 1) // per_page)
    page = max(0, min(page, total - 1))
    session["page"] = page

    start = page * per_page
    end = min(len(mailboxes), start + per_page)

    selected_names = [mailboxes[i] for i in sorted(selected) if 0 <= i < len(mailboxes)]
    selected_display = (
        ", ".join(selected_names) if selected_names else _("imap_picker_none_selected")
    )
    selected_display = _ellipsize(selected_display, 1200)

    text = (
        f"📁 <b>{_('manage_imap_folders')}</b>\n\n"
        f"{_('imap_picker_prompt')}\n\n"
        f"🔔 <b>{_('imap_monitored_folders')}</b>: <code>{html.escape(selected_display, quote=False)}</code>\n"
        f"📄 {_('imap_picker_page', current=page + 1, total=total)}"
    ).strip()

    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start, end):
        name = mailboxes[idx]
        prefix = "✅" if idx in selected else "⬜️"
        label = f"{prefix} {_ellipsize(name, 40)}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"account_mailboxes_toggle:{account_id}:{idx}".encode("utf-8")
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
                    data=f"account_mailboxes_page:{account_id}:{page - 1}".encode("utf-8")
                ),
            )
        )
    if page < total - 1:
        nav_row.append(
            InlineKeyboardButton(
                text=f"{_('imap_picker_next')} »",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"account_mailboxes_page:{account_id}:{page + 1}".encode("utf-8")
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
                    data=f"account_mailboxes_save:{account_id}".encode("utf-8")
                ),
            ),
            InlineKeyboardButton(
                text=f"✖️ {_('imap_picker_cancel')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"account_mailboxes:{account_id}".encode("utf-8")
                ),
            ),
        ]
    )

    await client.edit_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        clear_draft=False,
        reply_markup=ReplyMarkupInlineKeyboard(rows=rows),
    )


async def _render_account_mailboxes_menu(
    *,
    client: Client,
    chat_id: int,
    message_id: int,
    account_id: str,
    detected_preview: str | None = None,
) -> None:
    account_manager = AccountManager()
    account = account_manager.get_account(id=account_id)
    if not account:
        await client.edit_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"❌ {_('account_not_found')}",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            clear_draft=False,
        )
        return

    email = html.escape(account.get("email") or "", quote=False)
    boxes, source = _resolve_effective_mailboxes(account)
    boxes_display = html.escape(", ".join(boxes) if boxes else "INBOX", quote=False)

    base_text = (
        f"📁 <b>{_('manage_imap_folders')}</b>: {email}\n\n"
        f"🔔 <b>{_('imap_monitored_folders')}</b>: <code>{boxes_display}</code>\n"
        f"🧩 <b>{_('source')}</b>: {html.escape(source, quote=False)}"
    )

    text = base_text
    if detected_preview:
        candidate = f"{base_text}\n\n{detected_preview}".strip()
        if len(candidate) <= 4096:
            text = candidate
        else:
            text = f"{base_text}\n\n{_('content_truncated')}".strip()

    keyboard = [
        [
            InlineKeyboardButton(
                text=f"🔎 {_('imap_detect_folders')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"account_mailboxes_detect:{account_id}".encode("utf-8")
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"✏️ {_('imap_set_monitored_folders')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"account_mailboxes_set:{account_id}".encode("utf-8")
                ),
            ),
            InlineKeyboardButton(
                text=f"♻️ {_('imap_clear_override')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"account_mailboxes_clear:{account_id}".encode("utf-8")
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"« {_('manage_account')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"manage_account:{account_id}".encode("utf-8")
                ),
            )
        ],
    ]

    await client.edit_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        clear_draft=False,
        reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
    )


async def handle_accounts_callback(
    *, client: Client, update: UpdateNewCallbackQuery, data: str
) -> bool:
    chat_id = update.chat_id
    user_id = update.sender_user_id
    message_id = update.message_id

    account_manager = AccountManager()

    if data == "add_account":
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'add_account' callback query: {e}")
        await add_account_handler(client=client, update=update)
        return True

    if data.startswith("manage_account:"):
        account_id = data.split(":", 1)[1]
        account = account_manager.get_account(id=account_id)
        email = account["email"]
        logger.info(f"User {user_id} requested to manage account: {email}")

        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'manage_account' callback query: {e}")

        manage_text = f"🛠️ <b>{_('manage_account')}</b>: {email}"
        keyboard = [
            [
                InlineKeyboardButton(
                    text=f"📧 {_('manual_fetch_email')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"manual_fetch:{account_id}".encode("utf-8")
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"📁 {_('manage_imap_folders')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"account_mailboxes:{account_id}".encode("utf-8")
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"✏️ {_('edit_account')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"edit_account:{account_id}".encode("utf-8")
                    ),
                ),
                InlineKeyboardButton(
                    text=f"🗑️ {_('delete_account')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"delete_account_confirm:{account_id}".encode("utf-8")
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"« {_('back_to_accounts_list')}",
                    type=InlineKeyboardButtonTypeCallback(data=b"back_to_accounts"),
                )
            ],
        ]

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=manage_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
                reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
            )
        except Exception as e:
            logger.error(f"Failed to edit message for account management: {e}")
        return True

    if data.startswith("account_mailboxes:"):
        account_id = data.split(":", 1)[1]
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'account_mailboxes' callback query: {e}")

        _clear_picker_session(chat_id=chat_id, user_id=user_id, account_id=account_id)
        await _render_account_mailboxes_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
        )
        return True

    if data.startswith("account_mailboxes_detect:"):
        account_id = data.split(":", 1)[1]
        try:
            await client.api.answer_callback_query(
                update.id, text=_("imap_detecting"), url="", cache_time=1
            )
        except Exception as e:
            logger.warning(
                f"Failed to answer 'account_mailboxes_detect' callback query: {e}"
            )

        account = account_manager.get_account(id=account_id)
        if not account:
            await _render_account_mailboxes_menu(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                account_id=account_id,
                detected_preview=f"❌ {_('account_not_found')}",
            )
            return True

        detected_preview: str
        try:
            imap_client = IMAPClient(account)
            boxes = imap_client.list_mailboxes(selectable_only=False)
            selectable = [b["name"] for b in boxes if b.get("selectable")]
            noselect_count = len([b for b in boxes if not b.get("selectable")])

            lines: list[str] = []
            max_lines = 80
            max_preview_chars = 3000
            for name in selectable[:max_lines]:
                line = f"• <code>{html.escape(name, quote=False)}</code>"
                if len("\n".join(lines + [line])) > max_preview_chars:
                    break
                lines.append(line)

            extra = ""
            shown = len(lines)
            if len(selectable) > shown:
                extra = f"\n… (+{len(selectable) - shown} {_('more')})"
            noselect_note = ""
            if noselect_count > 0:
                noselect_note = f"\n({_('not_selectable')}: {noselect_count})"

            detected_preview = (
                f"<b>{_('imap_detected_folders')}</b>\n"
                + ("\n".join(lines) if lines else _("imap_no_folders_found"))
                + extra
                + noselect_note
            ).strip()
        except Exception as e:
            logger.error(f"Failed to detect mailboxes for account {account_id}: {e}")
            detected_preview = (
                f"❌ <b>{_('imap_detect_failed')}</b>\n"
                f"{html.escape(str(e), quote=False)[:1000]}"
            )

        await _render_account_mailboxes_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
            detected_preview=detected_preview,
        )
        return True

    if data.startswith("account_mailboxes_clear:"):
        account_id = data.split(":", 1)[1]
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(
                f"Failed to answer 'account_mailboxes_clear' callback query: {e}"
            )

        account_manager.update_account(
            id=account_id, updates={"imap_monitored_mailboxes": None}
        )

        await _render_account_mailboxes_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
        )
        return True

    if data.startswith("account_mailboxes_set:"):
        account_id = data.split(":", 1)[1]
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(
                f"Failed to answer 'account_mailboxes_set' callback query: {e}"
            )

        account = account_manager.get_account(id=account_id)
        if not account:
            await _render_account_mailboxes_menu(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                account_id=account_id,
                detected_preview=f"❌ {_('account_not_found')}",
            )
            return True

        try:
            imap_client = IMAPClient(account)
            boxes = imap_client.list_mailboxes(selectable_only=True)
            mailboxes = [b.get("name") for b in boxes if b.get("name")]
        except Exception as e:
            logger.error(f"Failed to list mailboxes for picker: {e}")
            await _render_account_mailboxes_menu(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                account_id=account_id,
                detected_preview=f"❌ <b>{_('imap_detect_failed')}</b>\n{html.escape(str(e), quote=False)[:1000]}",
            )
            return True

        mailboxes = [m for m in mailboxes if isinstance(m, str) and m.strip()]
        if not mailboxes:
            await _render_account_mailboxes_menu(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                account_id=account_id,
                detected_preview=f"❌ {_('imap_no_folders_found')}",
            )
            return True

        effective, _ = _resolve_effective_mailboxes(account)
        eff_lower = {str(x).strip().lower() for x in (effective or []) if str(x).strip()}
        selected: set[int] = {
            idx for idx, name in enumerate(mailboxes) if name.strip().lower() in eff_lower
        }
        if not selected:
            for idx, name in enumerate(mailboxes):
                if name.strip().lower() == "inbox":
                    selected.add(idx)
                    break

        _set_picker_session(
            chat_id=chat_id,
            user_id=user_id,
            account_id=account_id,
            session={
                "mailboxes": mailboxes,
                "selected": selected,
                "page": 0,
                "per_page": _MAILBOX_PICK_PER_PAGE,
            },
        )

        await _render_mailbox_picker(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            account_id=account_id,
        )
        return True

    if data.startswith("account_mailboxes_toggle:"):
        parts = data.split(":", 2)
        account_id = parts[1] if len(parts) > 1 else ""
        idx_str = parts[2] if len(parts) > 2 else ""
        try:
            idx = int(idx_str)
        except Exception:
            idx = -1

        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass

        session = _get_picker_session(chat_id=chat_id, user_id=user_id, account_id=account_id)
        if not session:
            await _render_account_mailboxes_menu(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                account_id=account_id,
                detected_preview=f"❌ {_('conversation_expired_or_not_found')}",
            )
            return True

        mailboxes: list[str] = session.get("mailboxes") or []
        if idx < 0 or idx >= len(mailboxes):
            return True

        selected: set[int] = set(session.get("selected") or set())
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)
        session["selected"] = selected

        await _render_mailbox_picker(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            account_id=account_id,
        )
        return True

    if data.startswith("account_mailboxes_page:"):
        parts = data.split(":", 2)
        account_id = parts[1] if len(parts) > 1 else ""
        page_str = parts[2] if len(parts) > 2 else ""
        try:
            page = int(page_str)
        except Exception:
            page = 0

        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass

        session = _get_picker_session(chat_id=chat_id, user_id=user_id, account_id=account_id)
        if not session:
            await _render_account_mailboxes_menu(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                account_id=account_id,
                detected_preview=f"❌ {_('conversation_expired_or_not_found')}",
            )
            return True

        session["page"] = max(0, page)
        await _render_mailbox_picker(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            account_id=account_id,
        )
        return True

    if data.startswith("account_mailboxes_save:"):
        account_id = data.split(":", 1)[1]
        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass

        session = _get_picker_session(chat_id=chat_id, user_id=user_id, account_id=account_id)
        if not session:
            await _render_account_mailboxes_menu(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                account_id=account_id,
                detected_preview=f"❌ {_('conversation_expired_or_not_found')}",
            )
            return True

        mailboxes: list[str] = session.get("mailboxes") or []
        selected: set[int] = set(session.get("selected") or set())
        selected_names = [
            mailboxes[i] for i in range(len(mailboxes)) if i in selected and mailboxes[i]
        ]
        value = _normalize_mailboxes_csv(",".join(selected_names)) if selected_names else ""

        account_manager.update_account(
            id=account_id,
            updates={"imap_monitored_mailboxes": value or None},
        )

        _clear_picker_session(chat_id=chat_id, user_id=user_id, account_id=account_id)

        await _render_account_mailboxes_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
        )
        return True

    if data.startswith("manual_fetch:"):
        account_id = data.split(":", 1)[1]
        account = account_manager.get_account(id=account_id)
        email = account["email"]
        logger.info(f"User {user_id} requested manual email fetch for account: {email}")

        try:
            await client.api.answer_callback_query(
                update.id, text=_("manual_fetch_processing"), url="", cache_time=1
            )
        except Exception as e:
            logger.warning(f"Failed to answer 'manual_fetch' callback query: {e}")

        await manual_fetch_email_handler(client, update, account_id)
        return True

    if data.startswith("edit_account:"):
        account_id = data.split(":", 1)[1]
        account = account_manager.get_account(id=account_id)
        email = account["email"]
        logger.info(f"User {user_id} requested to edit account: {email}")
        try:
            await client.api.answer_callback_query(
                update.id, text=_("starting_edit_process"), url="", cache_time=1
            )
        except Exception as e:
            logger.warning(f"Failed to answer 'edit_account' callback query: {e}")

        await edit_account_conversation_starter(client, update)
        return True

    if data.startswith("delete_account_confirm:"):
        account_id = data.split(":", 1)[1]
        account = account_manager.get_account(id=account_id)
        email = account["email"]
        logger.info(f"User {user_id} requested confirmation to delete account: {email}")

        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(
                f"Failed to answer 'delete_account_confirm' callback query: {e}"
            )

        confirm_text = f"""❓ <b>{_('delete_account_confirmation')}</b>

{_('are_you_sure_delete')} <b>{email}</b>?"""
        keyboard = [
            [
                InlineKeyboardButton(
                    text=f"✅ {_('yes_delete')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"delete_account_execute:{account_id}".encode("utf-8")
                    ),
                ),
                InlineKeyboardButton(
                    text=f"❌ {_('no_cancel')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"manage_account:{account_id}".encode("utf-8")
                    ),
                ),
            ]
        ]

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=confirm_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
                reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
            )
        except Exception as e:
            logger.error(f"Failed to edit message for delete confirmation: {e}")
        return True

    if data.startswith("delete_account_execute:"):
        account_id = data.split(":", 1)[1]
        account = account_manager.get_account(id=account_id)
        email = account["email"]
        logger.info(f"User {user_id} confirmed deletion for account: {email}")

        try:
            await client.api.answer_callback_query(
                update.id, text=_("processing"), url="", cache_time=1
            )
        except Exception as e:
            logger.warning(
                f"Failed to answer 'delete_account_execute' callback query: {e}"
            )

        success = account_manager.remove_account(id=account_id)

        if success:
            result_text = f"""✅ <b>{_('account_deleted_success')}</b>

{_('account')} <b>{email}</b> {_('deleted')}."""
        else:
            result_text = f"""❌ <b>{_('delete_account_fail')}</b>

{_('could_not_delete')} {email}. {_('already_deleted_or_error')}"""
            logger.warning(
                f"Failed to delete account {email} for user {user_id}. Might be already deleted."
            )

        keyboard = [
            [
                InlineKeyboardButton(
                    text=f"« {_('back_to_accounts_list')}",
                    type=InlineKeyboardButtonTypeCallback(data=b"back_to_accounts"),
                )
            ]
        ]

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=result_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
                reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
            )
        except Exception as e:
            logger.error(f"Failed to edit message after account deletion attempt: {e}")
        return True

    if data == "back_to_accounts":
        logger.info(f"User {user_id} requested to go back to accounts list")

        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'back_to_accounts' callback query: {e}")

        accounts = account_manager.get_all_accounts()

        message_text = f"📧 <b>{_('email_accounts_management')}</b>\n\n"
        keyboard_rows = []
        if accounts:
            message_text += f"<b>{_('select_account_to_manage')}:</b>\n"
            for account in accounts:
                button_text = account.get("alias") or account["email"]
                callback_data = f"manage_account:{account['id']}".encode("utf-8")
                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            text=button_text,
                            type=InlineKeyboardButtonTypeCallback(data=callback_data),
                        )
                    ]
                )
            message_text += "\n"
        else:
            message_text += f"{_('no_accounts')}\n\n"

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=_("add_account"),
                    type=InlineKeyboardButtonTypeCallback(data=b"add_account"),
                )
            ]
        )

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=message_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
                reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard_rows),
            )
        except Exception as e:
            logger.error(f"Failed to edit message to go back to accounts list: {e}")
        return True

    return False
