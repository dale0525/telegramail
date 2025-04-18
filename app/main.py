"""
Main entry point for TelegramMail application.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv
from app.bot.handlers.callback import callback_handler
from app.utils.logger import Logger
from aiotdlib import Client, ClientSettings
from aiotdlib.api import UpdateNewMessage, API, BotCommand, UpdateNewCallbackQuery
from app.bot.handlers.start import start_command_handler
from app.bot.handlers.help import help_command_handler

from app.bot.handlers.accounts import (
    accounts_management_command_handler,
)
from app.bot.handlers.message import message_handler
from app.i18n import _

load_dotenv()
logger = Logger().get_logger(__name__)


async def main():
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not api_id or not api_hash or not bot_token:
        logger.debug("No TELEGRAM_API_ID or TELEGRAM_API_HASH or TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    bot = Client(
        settings=ClientSettings(
            api_id=int(api_id),
            api_hash=api_hash,
            bot_token=bot_token,
            files_directory=os.path.join(os.path.dirname(__file__), "data"),
        )
    )

    # register /start command
    @bot.bot_command_handler(command="start")
    async def on_start_command(client: Client, update: UpdateNewMessage):
        await client.api.delete_messages(
            chat_id=update.message.chat_id,
            message_ids=[update.message.id],
            revoke=True,
        )
        await start_command_handler(client, update)

    # register /help command
    @bot.bot_command_handler(command="help")
    async def on_help_command(client: Client, update: UpdateNewMessage):
        await client.api.delete_messages(
            chat_id=update.message.chat_id,
            message_ids=[update.message.id],
            revoke=True,
        )
        await help_command_handler(client, update)

    # register /accounts command
    @bot.bot_command_handler(command="accounts")
    async def on_accounts_command(client: Client, update: UpdateNewMessage):
        await client.api.delete_messages(
            chat_id=update.message.chat_id,
            message_ids=[update.message.id],
            revoke=True,
        )
        await accounts_management_command_handler(client, update)

    # register message handler for all non-command messages
    async def on_update_new_message(client: Client, update: UpdateNewMessage):
        # Run the message handler in a background task to avoid blocking
        asyncio.create_task(message_handler(client, update))

    bot.add_event_handler(
        on_update_new_message,
        update_type=API.Types.UPDATE_NEW_MESSAGE,
    )

    # register button callback
    async def callback_query_handler(client: Client, update: UpdateNewCallbackQuery):
        await callback_handler(client, update)

    bot.add_event_handler(
        callback_query_handler, update_type=API.Types.UPDATE_NEW_CALLBACK_QUERY
    )

    async with bot:
        await bot.api.set_commands(
            [
                BotCommand(command="start", description=_("command_desc_start")),
                BotCommand(command="help", description=_("command_desc_help")),
                BotCommand(command="accounts", description=_("command_desc_accounts")),
            ]
        )
        await bot.idle()


if __name__ == "__main__":
    asyncio.run(main())
