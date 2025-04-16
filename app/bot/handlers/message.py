from aiotdlib import Client
from aiotdlib.api import UpdateNewMessage
from app.bot.conversation import Conversation
from app.bot.handlers.access import validate_admin
from app.utils.logger import Logger

logger = Logger().get_logger(__name__)


# handle non-command messages
async def message_handler(client: Client, update: UpdateNewMessage):
    """handle all non-command messages and route them to active conversations (if exists)"""
    logger.debug(f"receive message: {update}")
    if not validate_admin(update):
        return
    if not update.message.content.text:
        return  # ignore non-texts

    chat_id = update.message.chat_id
    user_id = update.message.sender_id.user_id

    # check if there's any active conversations
    conversation = Conversation.get_instance(chat_id, user_id)
    if conversation:
        handled = await conversation.handle_update(update)
        if handled:
            return
