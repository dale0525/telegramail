from aiotdlib import Client
from aiotdlib.api import (
    UpdateNewCallbackQuery,
    InlineKeyboardButton,
    ReplyMarkupInlineKeyboard,
    InlineKeyboardButtonTypeCallback,
    InputMessageText,
    FormattedText,
    LinkPreviewOptions,
)
from app.utils import Logger
from app.i18n import _
from app.bot.conversation import Conversation, ConversationState
from app.bot.handlers.accounts import (
    add_account_handler,
    edit_account_conversation_starter,
)
from app.email_utils.account_manager import AccountManager
from app.bot.utils import answer_callback

logger = Logger().get_logger(__name__)


async def callback_handler(client: Client, update: UpdateNewCallbackQuery):
    """handle button callback, routing to Conversation if active"""
    chat_id = update.chat_id
    user_id = update.sender_user_id
    data = update.payload.data.decode("utf-8")
    message_id = update.message_id  # Get message_id early for potential use
    logger.debug(f"receive button callback from {user_id} in {chat_id}, data: {data}")

    # Check if there is an active conversation for this user
    conversation = Conversation.get_instance(chat_id, user_id)
    if conversation and conversation.state == ConversationState.ACTIVE:
        logger.debug(f"Routing callback to active conversation for user {user_id}")
        handled_by_conv = await conversation.handle_callback_update(update)
        if handled_by_conv:
            # The conversation processed the callback (e.g., SSL selection)
            return
        else:
            logger.warning(
                f"Active conversation for user {user_id} did not handle callback data: {data}"
            )
            # Fall through to general handlers if conversation didn't handle it

    # --- General Callback Handling (if not handled by conversation) ---
    logger.debug(f"Handling callback as general action for user {user_id}")
    if data == "add_account":
        # Answer the callback query *first* to stop the loading animation
        try:
            # Add text="" to satisfy the API requirement
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'add_account' callback query: {e}")
        # Make sure to answer the callback query even if starting a new handler
        await add_account_handler(client=client, update=update)

    elif data.startswith("manage_account:"):
        # Handle the click on a specific account management button
        email = data.split(":", 1)[1]
        logger.info(f"User {user_id} requested to manage account: {email}")

        # Answer the callback query first
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'manage_account' callback query: {e}")

        # Edit the message to show Edit/Delete options for this account
        message_id = update.message_id
        manage_text = f"🛠️ <b>{_('manage_account')}</b>: {email}"
        keyboard = [
            [
                InlineKeyboardButton(
                    text=f"✏️ {_('edit_account')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"edit_account:{email}".encode("utf-8")
                    ),
                ),
                InlineKeyboardButton(
                    text=f"🗑️ {_('delete_account')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"delete_account_confirm:{email}".encode("utf-8")
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

    elif data.startswith("edit_account:"):
        # Start the conversation flow for editing an account
        email = data.split(":", 1)[1]
        logger.info(f"User {user_id} requested to edit account: {email}")
        try:
            # Answer immediately before starting potentially longer conversation setup
            await client.api.answer_callback_query(
                update.id, text=_("starting_edit_process"), url="", cache_time=1
            )
        except Exception as e:
            logger.warning(f"Failed to answer 'edit_account' callback query: {e}")
        # Call the function in accounts.py to handle the conversation
        await edit_account_conversation_starter(client, update)

    elif data.startswith("delete_account_confirm:"):
        # Ask for confirmation before deleting
        email = data.split(":", 1)[1]
        logger.info(f"User {user_id} requested confirmation to delete account: {email}")

        # Answer the callback query first
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(
                f"Failed to answer 'delete_account_confirm' callback query: {e}"
            )

        message_id = update.message_id
        confirm_text = f"""❓ <b>{_('delete_account_confirmation')}</b>

{_('are_you_sure_delete')} <b>{email}</b>?"""
        keyboard = [
            [
                InlineKeyboardButton(
                    text=f"✅ {_('yes_delete')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"delete_account_execute:{email}".encode("utf-8")
                    ),
                ),
                InlineKeyboardButton(
                    text=f"❌ {_('no_cancel')}",
                    type=InlineKeyboardButtonTypeCallback(
                        # Go back to the manage screen for this email
                        data=f"manage_account:{email}".encode("utf-8")
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

    elif data.startswith("delete_account_execute:"):
        # Execute the deletion
        email = data.split(":", 1)[1]
        logger.info(f"User {user_id} confirmed deletion for account: {email}")

        # Answer the callback query first
        try:
            # Using a generic text, as the message will be updated immediately
            await client.api.answer_callback_query(
                update.id, text=_("processing"), url="", cache_time=1
            )
        except Exception as e:
            logger.warning(
                f"Failed to answer 'delete_account_execute' callback query: {e}"
            )

        # Perform the deletion
        account_manager = AccountManager()
        success = account_manager.remove_account(email)

        # Prepare the result message and keyboard
        if success:
            result_text = f"""✅ <b>{_('account_deleted_success')}</b>

{_('account')} <b>{email}</b> {_('deleted')}."""
        else:
            # This might happen if the account was deleted elsewhere between confirmation and execution
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

    elif data == "back_to_accounts":
        # Go back to the main accounts list view
        logger.info(f"User {user_id} requested to go back to accounts list")

        # Answer the callback query first
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'back_to_accounts' callback query: {e}")

        # Fetch accounts again to rebuild the list
        account_manager = AccountManager()
        accounts = account_manager.get_all_accounts()
        message_id = update.message_id

        # Reuse the logic from accounts_management_command_handler to build the view
        message_text = f"📧 <b>{_('email_accounts_management')}</b>\n\n"
        keyboard_rows = []
        if accounts:
            message_text += f"<b>{_('select_account_to_manage')}:</b>\n"
            for account in accounts:
                button_text = account.get("alias") or account["email"]
                callback_data = f"manage_account:{account['email']}".encode("utf-8")
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

    # Add other general callback handlers here if needed
    # elif data == "other_action":
    else:
        logger.warning(f"Unhandled callback data for user {user_id}: {data}")
        try:
            # Answer with a generic message if the callback is not recognized
            await client.api.answer_callback_query(
                update.id, text=_("unknown_action"), url="", cache_time=1
            )
        except Exception as e:
            logger.warning(f"Failed to answer unrecognized callback query: {e}")
