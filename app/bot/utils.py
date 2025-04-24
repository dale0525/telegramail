from aiotdlib import Client
from aiotdlib.api import Update
import asyncio, os, signal, json
from app.bot.bot_client import BotClient
from app.user.user_client import UserClient
from app.utils import Logger
from typing import Optional
from aiotdlib.api import (
    ChatFolder,
    ChatFolderIcon,
    Chat,
    ChatFolderName,
    FormattedText,
    InputFileLocal,
    InputChatPhotoStatic,
)
from app.i18n import _
from app.bot.handlers.access import get_groups, save_groups

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


async def _delete_message_later(
    client: Client, chat_id: int, message_id: int, delay: int
):
    """Helper coroutine to delete a message after a delay."""
    try:
        await asyncio.sleep(delay)
        await client.api.delete_messages(
            chat_id=chat_id, message_ids=[message_id], revoke=True
        )
        logger.debug(
            f"Auto-deleted message {message_id} in chat {chat_id} after {delay}s."
        )
    except Exception as e:
        logger.error(
            f"Failed to auto-delete message {message_id} in chat {chat_id}: {e}"
        )


async def send_and_delete_message(
    client: Client,
    chat_id: int,
    text: str,
    delete_after_seconds: int | None = None,
) -> int | None:
    """
    Sends a text message and optionally schedules its deletion after a specified delay
    without blocking the caller.

    Args:
        client: The aiotdlib client instance.
        chat_id: The target chat ID.
        text: The message text to send.
        delete_after_seconds: The delay in seconds before deleting the message.
                              If None or 0, the message will not be deleted.

    Returns:
        The ID of the sent message if successful, otherwise None.
    """
    message_id = None
    try:
        msg = await client.send_text(
            chat_id=chat_id, text=text, disable_notification=True
        )
        message_id = msg.id
        logger.debug(f"Sent message {message_id} to chat {chat_id}.")

        if message_id and delete_after_seconds and delete_after_seconds > 0:
            asyncio.create_task(
                _delete_message_later(client, chat_id, message_id, delete_after_seconds)
            )
            logger.debug(
                f"Scheduled message {message_id} for deletion in {delete_after_seconds}s."
            )

    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")
        return None

    return message_id


async def shutdown(client: Client, loop: asyncio.AbstractEventLoop):
    """handle graceful shutdown"""
    logger.info("Shutting down...")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


def setup_signal_handlers(client: Client, loop: asyncio.AbstractEventLoop):
    """set up signal handlers for graceful shutdown"""
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(shutdown(client, loop))
        )


async def _ensure_groups_in_folder(folder: ChatFolder):
    groups = get_groups()
    if groups is None:
        return
    client = UserClient().client
    edited_folder = folder.model_copy()
    groups_in_folder = edited_folder
    for email, group_id in groups.items():
        if group_id not in groups_in_folder:
            groups_in_folder.append(group_id)
    try:
        await client.api.edit_chat_folder(
            chat_folder_id=folder.id, folder=edited_folder
        )
    except Exception as e:
        logger.error(f"Failed to edit chat folder: {e}")


async def _create_email_folder(folder_name: str, icon_name: str = "Work") -> ChatFolder:
    """
    Create a new chat folder.
    """
    try:
        groups = get_groups()
        if groups is None:
            return None
        client = UserClient().client
        folder_info = await client.api.create_chat_folder(
            ChatFolder(
                name=ChatFolderName(text=FormattedText(text=folder_name, entities=[])),
                icon=ChatFolderIcon(name=icon_name),
                pinned_chat_ids=[],
                included_chat_ids=list(groups.values()),
                excluded_chat_ids=[],
                color_id=-1,
            )
        )
        folder = await client.api.get_chat_folder(folder_info.id)
        return folder
    except Exception as e:
        logger.error(f"Failed to create chat folder '{folder_name}': {e}")
        return None


async def get_email_folder_id() -> tuple[bool, str, Optional[int]]:
    """
    Get folder id of "Email". If not exists, create one.

    Returns:
        Returns:
        Tuple (success: bool, message: str, folder_id: Optional[int])
        folder_id is the ID if creation was successful, otherwise None.
    """
    client = UserClient().client
    folder_name = "Email"
    folder_id = None
    folder = None
    # get folder id from data/folder.txt
    folder_file_path = os.path.join(os.getcwd(), "data", "folder.txt")
    if os.path.exists(folder_file_path):
        with open(folder_file_path, "r") as f:
            folder_id = int(f.read().strip())

    if folder_id and folder_id > 0:
        try:
            folder = await client.api.get_chat_folder(folder_id)
            await _ensure_groups_in_folder(folder=folder)
        except Exception as e:
            logger.error(
                f"Failed to get chat folder '{folder_name}' with ID {folder_id}: {e}"
            )
            folder = None

    if not folder:
        folder = await _create_email_folder(folder_name=folder_name)
        if folder:
            await _ensure_groups_in_folder(folder=folder)
            folder_id = folder.id
            with open(folder_file_path, "w+") as f:
                f.write(str(folder_id))

    if folder_id:
        return True, _("folder_create_success")
    else:
        return False, _("folder_create_fail")


async def _create_super_group(name: str, desc: str) -> Chat:
    """
    Create a super group with given name and desc.
    The group is a forum.

    Args:
        name: Group title. Should be email alias
        desc: Group description. Should be email address

    Returns:
        The group as Chat object.
    """
    client = UserClient().client
    group = await client.api.create_new_supergroup_chat(
        title=name, is_forum=True, description=desc, message_auto_delete_time=0
    )
    photo_path = None
    if "@gmail.com" in desc:
        photo_path = os.path.join(os.getcwd(), "app", "resources", "icons", "gmail.jpg")
    if photo_path:
        await client.api.set_chat_photo(
            chat_id=group.id,
            photo=InputChatPhotoStatic(photo=InputFileLocal(path=photo_path)),
        )
    bot_id = await BotClient().client.get_my_id()
    await client.api.add_chat_members(chat_id=group.id, user_ids=[bot_id])
    return group


async def get_group_id(
    email: str,
    alias: Optional[str] = None,
) -> tuple[bool, str, Optional[int]]:
    """
    Get group id of the email account. If not exists, create one.

    Args:
        email: email address
        alias: email acocunt alias

    Returns:
        Tuple (success: bool, message: str, group_id: Optional[int])
        group_id is the ID if creation was successful, otherwise None.
    """

    group_id = None
    group = None
    groups = get_groups()
    if groups is None:
        groups = {}
    else:
        if email in groups:
            group_id = groups[email]

    if group_id and group_id > 0:
        try:
            client = UserClient().client
            group = await client.api.get_chat(group_id)
        except Exception as e:
            logger.error(f"Failed to get group {group_id}: {e}")

    if not group:
        group = await _create_super_group(name=alias, desc=email)
        if group:
            group_id = group.id
            groups[email] = group_id
            save_groups(groups)

    if group_id:
        return True, _("group_create_success")
    else:
        return False, _("group_create_fail")
