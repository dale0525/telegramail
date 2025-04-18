from app.i18n import _
from app.email_utils.account_manager import AccountManager
from app.bot.handlers.access import validate_admin
from app.bot.handlers.help import help_command_handler
from app.bot.handlers.accounts import add_account_handler
from app.bot.utils import send_and_delete_message

from aiotdlib import Client
from aiotdlib.api import UpdateNewMessage


async def start_command_handler(client: Client, update: UpdateNewMessage):
    """handle /start command"""
    if not validate_admin(update):
        return

    account_manager = AccountManager.get_instance()
    if len(account_manager.accounts) <= 0:
        await send_and_delete_message(
            client=client,
            chat_id=update.message.chat_id,
            text=_("no_accounts"),
            delete_after_seconds=5,
        )
        await add_account_handler(
            client=client,
            chat_id=update.message.chat_id,
            user_id=update.message.sender_id.user_id,
        )
    else:
        await help_command_handler(client, update)
