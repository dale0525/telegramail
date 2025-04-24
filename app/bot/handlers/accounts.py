from app.i18n import _
from app.bot.conversation import Conversation
from app.email_utils.account_manager import AccountManager
from app.utils import Logger
from app.bot.handlers.access import validate_admin

# Import the step definitions
from .account_steps import ADD_ACCOUNT_STEPS, EDIT_ACCOUNT_STEPS

from aiotdlib import Client
from aiotdlib.api import (
    UpdateNewMessage,
    ReplyMarkupInlineKeyboard,
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    UpdateNewCallbackQuery,
)

logger = Logger().get_logger(__name__)


async def accounts_management_command_handler(client: Client, update: UpdateNewMessage):
    """handle /accounts command"""
    if not validate_admin(update):
        return
    chat_id = update.message.chat_id
    # user_id = update.message.sender_id.user_id # Not used currently

    # get all email accounts
    account_manager = AccountManager()
    accounts = account_manager.get_all_accounts()

    # build accounts list message
    message_text = f"📧 <b>{_('email_accounts_management')}</b>\n\n"

    keyboard_rows = []
    if accounts:
        message_text += f"<b>{_('select_account_to_manage')}:</b>\n"
        for account in accounts:
            # Use alias if available, otherwise use email
            button_text = account.get("alias") or account["email"]
            # Encode email into callback data, prefixed for routing
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

    # Add the "Add Account" button at the end
    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text=_("add_account"),
                type=InlineKeyboardButtonTypeCallback(data=b"add_account"),
            )
        ]
    )
    await client.send_text(
        chat_id,
        message_text,
        reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard_rows),
    )


async def add_account_handler(
    client: Client,
    update: UpdateNewCallbackQuery | None = None,
    chat_id: int | None = None,
    user_id: int | None = None,
):
    """handle add account button using steps from account_steps.py"""
    if update:
        chat_id = update.chat_id
        user_id = update.sender_user_id

    # Use the imported steps
    steps = ADD_ACCOUNT_STEPS

    conversation = await Conversation.create_conversation(
        client, chat_id, user_id, steps
    )

    # when conversation ends
    async def on_complete(context):
        # NOTE: check_common_provider logic is now within the steps/post_process
        # No need to check context["use_common_provider"] explicitly here
        # The context should already contain the correct server/port/ssl values

        # add account using the full context
        account_manager = AccountManager()

        success = account_manager.add_account(
            {
                "email": context["email"],
                "password": context["password"],
                "smtp_server": context["smtp_server"],
                "smtp_port": context["smtp_port"],
                "smtp_ssl": context["smtp_ssl"],
                "imap_server": context["imap_server"],
                "imap_port": context["imap_port"],
                "imap_ssl": context["imap_ssl"],
                "alias": context["alias"],
            }
        )

        if success:
            alias = context.get(
                "alias", context.get("email")
            )  # Fallback to email if alias not set
            conversation.finish_message = (
                f"✅ <b>{alias}</b> {_('add_account_success')}"
            )
            conversation.finish_message_type = "success"
            conversation.finish_message_delete_after = 3
        else:
            # Assuming failure means the email already exists based on AccountManager logic
            conversation.finish_message = f"❌ {_('add_account_fail_exists')}"
            conversation.finish_message_type = "error"
            conversation.finish_message_delete_after = 5

    conversation.on_finish(on_complete)

    # Start the conversation
    await conversation.start()


# --- New function to start the edit conversation ---
async def edit_account_conversation_starter(
    client: Client, update: UpdateNewCallbackQuery
):
    """Starts the conversation to edit an existing email account."""
    chat_id = update.chat_id
    user_id = update.sender_user_id
    message_id = update.message_id  # Keep track for potential cleanup
    # Extract email from callback data (e.g., "edit_account:user@example.com")
    try:
        email = update.payload.data.decode("utf-8").split(":", 1)[1]
    except (IndexError, UnicodeDecodeError) as e:
        logger.error(
            f"Could not extract email from edit callback data: {update.payload.data}. Error: {e}"
        )
        # Optionally send an error message to the user
        await client.send_text(chat_id, _("error_starting_edit_missing_email"))
        return

    account_manager = AccountManager()
    current_account_data = account_manager.get_account(email)

    if not current_account_data:
        logger.error(
            f"Attempted to edit non-existent account: {email} by user {user_id}"
        )
        # Edit the original message to indicate failure
        try:
            await client.api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ {_('error_edit_account_not_found', email=email)}",
                # Add a back button maybe?
                reply_markup=ReplyMarkupInlineKeyboard(
                    rows=[
                        [
                            InlineKeyboardButton(
                                text=f"« {_('back_to_accounts_list')}",
                                type=InlineKeyboardButtonTypeCallback(
                                    data=b"back_to_accounts"
                                ),
                            )
                        ]
                    ]
                ),
            )
        except Exception as e_edit:
            logger.error(
                f"Failed to edit message for account not found error: {e_edit}"
            )
        return

    # Use the imported steps for editing
    steps = EDIT_ACCOUNT_STEPS

    # Create conversation, passing existing data as the initial context
    conversation = await Conversation.create_conversation(
        client,
        chat_id,
        user_id,
        steps,
        context=current_account_data.copy(),
    )

    # When conversation ends (successfully or cancelled)
    async def on_complete(context):
        updated_data = {}
        # Collect only the keys that were actually provided by the user during the conversation
        # This avoids overwriting existing values with None if a step was skipped.
        # Assumes Conversation context stores *only* the user-provided values for optional steps.
        # Alternatively, compare context with initial_context.
        initial_keys = current_account_data.keys()
        for key, value in context.items():
            # Only include keys defined in EDIT_ACCOUNT_STEPS or the original email
            # And only if the value is different from the initial value OR if it's a newly provided optional value
            # A simpler approach: Assume context contains *only* the changes.
            # Check if the key is an editable field
            is_editable_field = any(step["key"] == key for step in EDIT_ACCOUNT_STEPS)
            if (
                is_editable_field and key in context
            ):  # Check if user provided input for this key
                updated_data[key] = value

        if not updated_data:
            logger.info(f"Edit conversation for {email} completed with no changes.")
            conversation.finish_message = (
                f"ℹ️ {_('edit_account_no_changes', email=email)}"
            )
            conversation.finish_message_type = "info"
            conversation.finish_message_delete_after = 3
            return  # Skip saving if no changes

        logger.info(f"Updating account {email} with data: {updated_data}")
        success = account_manager.update_account(email, updated_data)

        if success:
            alias = updated_data.get("alias", current_account_data.get("alias", email))
            conversation.finish_message = (
                f"✅ <b>{alias}</b> {_('edit_account_success')}"
            )
            conversation.finish_message_type = "success"
            conversation.finish_message_delete_after = 3
        else:
            # Update usually shouldn't fail unless there's an I/O error
            conversation.finish_message = f"❌ {_('edit_account_fail', email=email)}"
            conversation.finish_message_type = "error"
            conversation.finish_message_delete_after = 5

    conversation.on_finish(on_complete)

    # Add a handler for cancellation if your Conversation class supports it
    async def on_cancel():
        logger.info(f"Edit conversation for {email} cancelled by user {user_id}.")
        # You might want to edit the original message or send a cancellation confirmation
        # For now, let the conversation handle its cleanup.
        pass

    conversation.on_cancel(on_cancel)

    # Start the conversation
    logger.info(f"Starting edit conversation for account: {email} for user {user_id}")
    await conversation.start()


async def remove_account_command_handler(client: Client, update: UpdateNewMessage):
    """处理 /remove 命令，引导用户删除邮箱账户"""
    chat_id = update.message.chat_id
    user_id = update.message.sender_id.user_id

    # 获取所有账户
    account_manager = AccountManager()
    accounts = account_manager.get_all_accounts()

    if not accounts:
        await client.send_text(chat_id, "❌ 您没有添加任何邮箱账户。")
        return

    # 构建账户选择消息
    account_list = "\n".join(
        [f"{i}. {account['email']}" for i, account in enumerate(accounts, 1)]
    )

    # 定义步骤
    steps = [
        {
            "text": f"📧 <b>删除邮箱账户</b>\n\n当前账户:\n{account_list}\n\n请输入要删除的账户编号:",
            "key": "account_index",
            "validate": lambda x: (
                x.isdigit() and 1 <= int(x) <= len(accounts),
                "请输入有效的账户编号!",
            ),
            "process": lambda x: int(x) - 1,  # 转换为从0开始的索引
        }
    ]

    # 创建对话实例
    conversation = await Conversation.create_conversation(
        client, chat_id, user_id, steps
    )

    # 注册完成处理程序
    async def on_complete(context):
        index = context["account_index"]
        email = accounts[index]["email"]

        # 删除账户
        success = account_manager.remove_account(email)

        # 配置通用结束提示信息和自动删除时间（如 5 秒后自动删除）
        if success:
            conversation.finish_message = f"✅ 邮箱账户 <b>{email}</b> 已成功删除!"
            conversation.finish_message_type = "success"
            conversation.finish_message_delete_after = 5
        else:
            conversation.finish_message = f"❌ 删除邮箱账户失败。"
            conversation.finish_message_type = "error"
            conversation.finish_message_delete_after = 5

    conversation.on_finish(on_complete)

    # 开始对话
    await conversation.start()
