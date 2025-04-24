from aiotdlib import Client
from aiotdlib.api import ChatEventForumTopicDeleted
from app.bot.conversation import Conversation
from app.bot.handlers.access import validate_admin
from app.utils import Logger

logger = Logger().get_logger(__name__)


# handle non-command messages
async def email_deleted_handler(client: Client, update: ChatEventForumTopicDeleted):
    logger.debug(f"receive message: {update}")
