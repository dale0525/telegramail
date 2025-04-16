from aiotdlib import Client
from aiotdlib.api import UpdateNewMessage
from app.bot.handlers.access import validate_admin
from app.i18n import _


async def help_command_handler(client: Client, update: UpdateNewMessage):
    """handle /help command"""
    if not validate_admin(update):
        return
    await client.send_text(update.message.chat_id, _("help_text"))
