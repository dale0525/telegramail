"""
Main entry point for TelegramMail application.
"""

import asyncio
import os
import sys
import signal
from dotenv import load_dotenv
from app.bot.bot_client import BotClient
from app.user.user_client import UserClient
from app.utils import Logger
from aiotdlib import Client
from aiotdlib.api import (
    UpdateNewMessage,
    API,
    BotCommand,
    UpdateNewCallbackQuery,
)
from app.bot.handlers.start import start_command_handler
from app.bot.handlers.help import help_command_handler
from app.bot.handlers.callback import callback_handler
from app.bot.handlers.access import get_phone
from app.bot.handlers.accounts import (
    accounts_management_command_handler,
)
from app.bot.handlers.check_email import check_command_handler
from app.bot.handlers.message import message_handler
from app.bot.handlers.test import test_command_handler
from app.cron.email_check_scheduler import start_email_check_scheduler
from app.i18n import _

load_dotenv()
logger = Logger().get_logger(__name__)

# Global variable to store the user client instance
user_client_instance = None


# Handle graceful shutdown
async def shutdown(signal, loop):
    """
    Handle application shutdown gracefully
    """
    logger.info(f"Received exit signal {signal.name}...")

    # Stop the user client if it's running
    global user_client_instance
    if user_client_instance:
        logger.info("Stopping user client...")
        await user_client_instance.stop()

    # Cancel all running tasks
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    logger.info(f"Cancelling {len(tasks)} outstanding tasks")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    loop.stop()


async def main():

    # ---------- RUN BOT ----------#
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not api_id or not api_hash or not bot_token:
        logger.debug("No TELEGRAM_API_ID or TELEGRAM_API_HASH or TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    # Set up signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, lambda s=s: asyncio.create_task(shutdown(s, loop)))

    bot = BotClient().client

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

    # register /check command
    @bot.bot_command_handler(command="check")
    async def on_check_command(client: Client, update: UpdateNewMessage):
        await client.api.delete_messages(
            chat_id=update.message.chat_id,
            message_ids=[update.message.id],
            revoke=True,
        )
        await check_command_handler(client, update)

    # register /test command
    @bot.bot_command_handler(command="test")
    async def on_test_command(client: Client, update: UpdateNewMessage):
        await client.api.delete_messages(
            chat_id=update.message.chat_id,
            message_ids=[update.message.id],
            revoke=True,
        )
        await test_command_handler(client, update)

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

    phone_number = get_phone()
    if phone_number:
        asyncio.create_task(run_user(phone_number=phone_number))

    async with bot:
        await bot.api.set_commands(
            [
                BotCommand(command="start", description=_("command_desc_start")),
                BotCommand(command="help", description=_("command_desc_help")),
                BotCommand(command="accounts", description=_("command_desc_accounts")),
                BotCommand(command="check", description=_("command_desc_check")),
            ]
        )

        # Start periodic email checking task (every 5 minutes)
        start_email_check_scheduler()

        await bot.idle()


async def run_user(phone_number: str):
    global user_client_instance
    user_client = UserClient()
    user_client_instance = user_client
    user_client.start(phone_number)
    user = user_client.client

    # register message handler for ChatEventForumTopicDeleted messages
    # async def on_forum_deleted(client: Client, update: BaseObject):
    #     # Run the message handler in a background task to avoid blocking
    #     asyncio.create_task(email_deleted_handler(client, update))

    # user.add_event_handler(
    #     on_forum_deleted,
    #     update_type=API.Types.ANY,
    # )
    async with user:
        logger.info(f"Started user client with phone number: {phone_number}")
        await user.idle()


if __name__ == "__main__":
    asyncio.run(main())
