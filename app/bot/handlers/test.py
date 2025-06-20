from aiotdlib import Client
from aiotdlib.api import (
    UpdateNewMessage,
    ReplyMarkupInlineKeyboard,
    InlineKeyboardButton,
    InlineKeyboardButtonTypeUrl,
    InputMessageText,
    FormattedText,
    TextParseModeHTML,
    TextParseModeMarkdown,
    MessageSendOptions,
    ChatEventLogFilters,
    ChatEvent,
    ChatEventAction,
    ForumTopicInfo,
)
from app.bot.bot_client import BotClient
from app.bot.handlers.access import validate_admin
from app.email_utils.imap_client import IMAPClient
from app.i18n import _
from typing import Any
import datetime  # Import datetime for time comparison
from app.database import DBManager

from app.user.user_client import UserClient
from app.cron.email_delete_listener import check_all_deleted_topics


async def test_command_handler(client: Client, update: UpdateNewMessage):
    """handle /test command"""
    if not validate_admin(update):
        return
    await test()


async def test():
    icons = await BotClient().client.api.get_forum_topic_default_icons()
    print(icons)
