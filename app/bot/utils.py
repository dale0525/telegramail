from aiotdlib import Client
from aiotdlib.api import Update
import asyncio
from app.utils.logger import Logger

logger = Logger().get_logger(__name__)


async def answer_callback(
    client: Client, update: Update | None = None, callback_query_id: int | None = None
):
    if update:
        await client.api.answer_callback_query(
            callback_query_id=update.id, text="", url="", cache_time=0
        )
    elif callback_query_id:
        await client.api.answer_callback_query(
            callback_query_id=callback_query_id, text="", url="", cache_time=0
        )


async def send_and_delete_message(
    client: Client,
    chat_id: int,
    text: str,
    delete_after_seconds: int | None = None,
) -> int | None:
    """
    Sends a text message and optionally deletes it after a specified delay.

    Args:
        client: The aiotdlib client instance.
        chat_id: The target chat ID.
        text: The message text to send.
        delete_after_seconds: The delay in seconds before deleting the message.
                              If None or 0, the message will not be deleted.

    Returns:
        The ID of the sent message if successful, otherwise None.
        Returns None even if deletion fails after successful sending.
    """
    message_id = None
    try:
        msg = await client.send_text(
            chat_id=chat_id, text=text, disable_notification=True
        )
        message_id = msg.id
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")
        return None

    if message_id and delete_after_seconds and delete_after_seconds > 0:
        try:
            await asyncio.sleep(delete_after_seconds)
            await client.api.delete_messages(
                chat_id=chat_id, message_ids=[message_id], revoke=True
            )
            # Return the original message_id even if deletion is successful
        except Exception as e:
            logger.error(
                f"Failed to auto-delete message {message_id} in chat {chat_id}: {e}"
            )
            # Still return the original message_id, as sending was successful

    return message_id
