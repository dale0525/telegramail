import html

from aiotdlib import Client
from aiotdlib.api import (
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    LinkPreviewOptions,
    ReplyMarkupInlineKeyboard,
    UpdateNewCallbackQuery,
)

from app.bot.conversation import Conversation
from app.bot.utils import answer_callback
from app.email_utils.account_manager import AccountManager
from app.email_utils.signatures import (
    CHOICE_DEFAULT,
    add_account_signature,
    get_account_last_signature_choice,
    list_account_signatures,
    normalize_signature_choice,
    remove_account_signature,
    set_account_last_signature_choice,
    set_default_account_signature,
)
from app.i18n import _

_MAX_SIGNATURE_BUTTONS = 12


def _ellipsize(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if max_len <= 0:
        return ""
    if len(t) <= max_len:
        return t
    if max_len == 1:
        return "…"
    return t[: max_len - 1].rstrip() + "…"


def _render_signature_list(raw: str | None) -> str:
    items, default_id = list_account_signatures(raw)
    if not items:
        return _("signature_not_set")

    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        marker = "⭐" if item["id"] == default_id else "•"
        name = html.escape(_ellipsize(item.get("name") or "", 40), quote=False)
        preview = html.escape(_ellipsize(item.get("markdown") or "", 120), quote=False)
        lines.append(f"{marker} {idx}. <b>{name}</b>")
        lines.append(f"<code>{preview}</code>")
    return "\n".join(lines).strip()


async def _render_account_signature_menu(
    *,
    client: Client,
    chat_id: int,
    message_id: int,
    account_id: str,
    account_manager: AccountManager,
) -> None:
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
    text = (
        f"✍️ <b>{_('manage_signature')}</b>: {email}\n\n"
        f"{_('signature_markdown_auto_html')}\n\n"
        f"🧾 <b>{_('current_signature')}</b>:\n"
        f"{_render_signature_list(account.get('signature'))}"
    )

    rows = [
        [
            InlineKeyboardButton(
                text=f"➕ {_('signature_add')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"account_signature_add:{account_id}".encode("utf-8")
                ),
            ),
            InlineKeyboardButton(
                text=f"🧹 {_('signature_clear')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"account_signature_clear:{account_id}".encode("utf-8")
                ),
            ),
        ]
    ]

    items, _default_id = list_account_signatures(account.get("signature"))
    for idx, item in enumerate(items[:_MAX_SIGNATURE_BUTTONS], start=1):
        sid = item["id"]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⭐ {_('signature_use_default')} #{idx}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"account_signature_default:{account_id}:{sid}".encode(
                            "utf-8"
                        )
                    ),
                ),
                InlineKeyboardButton(
                    text=f"🗑 {_('signature_delete')} #{idx}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"account_signature_delete:{account_id}:{sid}".encode(
                            "utf-8"
                        )
                    ),
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text=f"« {_('manage_account')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"manage_account:{account_id}".encode("utf-8")
                ),
            )
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


async def _start_add_signature_conversation(
    *,
    client: Client,
    chat_id: int,
    user_id: int,
    message_id: int,
    account_id: str,
    account_manager: AccountManager,
) -> None:
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

    steps = [
        {
            "text": _("signature_input_name"),
            "key": "signature_name",
        },
        {
            "text": f"{_('signature_input_prompt')}\n\n{_('signature_markdown_auto_html')}",
            "key": "signature_markdown",
        },
    ]
    conversation = await Conversation.create_conversation(
        client=client,
        chat_id=chat_id,
        user_id=user_id,
        steps=steps,
        context={"account_id": int(account_id)},
    )

    async def on_complete(context):
        name = (context.get("signature_name") or "").strip()
        markdown = (context.get("signature_markdown") or "").strip()
        if not name or not markdown:
            conversation.finish_message = f"❌ {_('signature_save_failed')}"
            conversation.finish_message_type = "error"
            conversation.finish_message_delete_after = 5
        else:
            current_raw = account.get("signature")
            new_raw, _signature_id = add_account_signature(
                current_raw,
                name=name,
                markdown=markdown,
            )
            ok = account_manager.update_account(
                id=account_id,
                updates={"signature": new_raw},
            )
            if ok:
                conversation.finish_message = f"✅ {_('signature_saved')}"
                conversation.finish_message_type = "success"
                conversation.finish_message_delete_after = 3
            else:
                conversation.finish_message = f"❌ {_('signature_save_failed')}"
                conversation.finish_message_type = "error"
                conversation.finish_message_delete_after = 5

        await _render_account_signature_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
            account_manager=account_manager,
        )

    conversation.on_finish(on_complete)
    await conversation.start()


async def handle_account_signature_callback(
    *,
    client: Client,
    update: UpdateNewCallbackQuery,
    data: str,
    account_manager: AccountManager,
) -> bool:
    chat_id = update.chat_id
    user_id = update.sender_user_id
    message_id = update.message_id

    if data.startswith("account_signature_add:"):
        account_id = data.split(":", 1)[1]
        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass

        await _start_add_signature_conversation(
            client=client,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            account_id=account_id,
            account_manager=account_manager,
        )
        return True

    if data.startswith("account_signature_default:"):
        try:
            _p = data.split(":", 2)
            account_id = _p[1]
            signature_id = _p[2]
        except Exception:
            return True
        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass
        account = account_manager.get_account(id=account_id) or {}
        new_raw = set_default_account_signature(account.get("signature"), signature_id)
        account_manager.update_account(id=account_id, updates={"signature": new_raw})
        normalized_last = normalize_signature_choice(
            new_raw,
            get_account_last_signature_choice(account_id=int(account_id)),
        )
        set_account_last_signature_choice(
            account_id=int(account_id),
            choice=normalized_last,
        )
        await _render_account_signature_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
            account_manager=account_manager,
        )
        return True

    if data.startswith("account_signature_delete:"):
        try:
            _p = data.split(":", 2)
            account_id = _p[1]
            signature_id = _p[2]
        except Exception:
            return True
        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass
        account = account_manager.get_account(id=account_id) or {}
        new_raw = remove_account_signature(account.get("signature"), signature_id)
        account_manager.update_account(id=account_id, updates={"signature": new_raw})
        normalized_last = normalize_signature_choice(
            new_raw,
            get_account_last_signature_choice(account_id=int(account_id)),
        )
        set_account_last_signature_choice(
            account_id=int(account_id),
            choice=normalized_last,
        )
        await _render_account_signature_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
            account_manager=account_manager,
        )
        return True

    if data.startswith("account_signature_clear:"):
        account_id = data.split(":", 1)[1]
        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass

        account_manager.update_account(
            id=account_id,
            updates={"signature": None},
        )
        set_account_last_signature_choice(
            account_id=int(account_id),
            choice=CHOICE_DEFAULT,
        )

        await _render_account_signature_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
            account_manager=account_manager,
        )
        return True

    if data.startswith("account_signature:"):
        account_id = data.split(":", 1)[1]
        try:
            await answer_callback(client=client, update=update)
        except Exception:
            pass

        await _render_account_signature_menu(
            client=client,
            chat_id=chat_id,
            message_id=message_id,
            account_id=account_id,
            account_manager=account_manager,
        )
        return True

    return False
