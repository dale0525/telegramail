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
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


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

