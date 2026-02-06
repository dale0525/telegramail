from aiotdlib import Client
from aiotdlib.api import UpdateNewCallbackQuery

from app.bot.conversation import Conversation, ConversationState
from app.bot.handlers.callbacks.accounts import handle_accounts_callback
from app.bot.handlers.callbacks.drafts import handle_draft_callback
from app.bot.handlers.callbacks.emails import handle_email_action_callback
from app.bot.handlers.callbacks.labels import handle_label_callback
from app.bot.handlers.callbacks.identity_suggestions import (
    handle_identity_suggestion_callback,
)
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def callback_handler(client: Client, update: UpdateNewCallbackQuery):
    """handle button callback, routing to Conversation if active"""
    chat_id = update.chat_id
    user_id = update.sender_user_id
    data = update.payload.data.decode("utf-8")
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

    logger.debug(f"Handling callback as general action for user {user_id}")

    handlers = (
        handle_accounts_callback,
        handle_identity_suggestion_callback,
        handle_draft_callback,
        handle_label_callback,
        handle_email_action_callback,
    )

    for handler in handlers:
        handled = await handler(client=client, update=update, data=data)
        if handled:
            return

    logger.warning(f"Unhandled callback data for user {user_id}: {data}")
    try:
        await client.api.answer_callback_query(
            update.id, text=_("unknown_action"), url="", cache_time=1
        )
    except Exception as e:
        logger.warning(f"Failed to answer unrecognized callback query: {e}")
