from aiotdlib import Client
from aiotdlib.api import Update
import asyncio, os, signal, json
from app.utils.logger import Logger
from typing import Optional
from aiotdlib.api import (
    ChatAdministratorRights,
    ChatMemberStatusAdministrator,
    ChatFolder,
    ChatFolderIcon,
    Chat,
)
from app.i18n import _

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


# --- Telegram Specific Actions ---

# Default admin rights for new supergroups
DEFAULT_ADMIN_RIGHTS = ChatAdministratorRights(
    can_manage_chat=True,
    can_change_info=True,
    can_post_messages=True,
    can_edit_messages=True,
    can_delete_messages=True,
    can_invite_users=True,
    can_restrict_members=True,
    can_pin_messages=True,
    can_promote_members=False,  # Usually bots/users shouldn't promote others by default
    can_manage_video_chats=True,
    is_anonymous=False,
)


async def _create_chat_folder(
    client: Client, folder_name: str, icon_name: str = "Work"
) -> ChatFolder:
    """
    Create a new chat folder.
    """
    try:
        folder = await client.api.create_chat_folder(
            ChatFolder(name=folder_name, icon=ChatFolderIcon(name=icon_name))
        )
        return folder
    except Exception as e:
        logger.error(f"Failed to create chat folder '{folder_name}': {e}")
        return None


async def get_email_folder_id(client: Client) -> tuple[bool, str, Optional[int]]:
    """
    Get folder id of "Email". If not exists, create one.

    Args:
        client: The aiotdlib client instance.

    Returns:
        Returns:
        Tuple (success: bool, message: str, folder_id: Optional[int])
        folder_id is the ID if creation was successful, otherwise None.
    """
    folder_name = "Email"
    folder_id = None
    # get folder id from data/folder.txt
    folder_file_path = os.path.join(
        os.path.dirname(__file__), "..", "data", "folder.txt"
    )
    if os.path.exists(folder_file_path):
        with open(folder_file_path, "r") as f:
            folder_id = int(f.read().strip())

    if folder_id and folder_id > 0:
        try:
            folder = await client.api.get_chat_folder(folder_id)
        except Exception as e:
            logger.error(
                f"Failed to get chat folder '{folder_name}' with ID {folder_id}: {e}"
            )
            folder = None

    if not folder:
        folder = await _create_chat_folder(client, folder_name)
        if folder:
            folder_id = folder.id
            with open(folder_file_path, "w+") as f:
                f.write(str(folder_id))

    if folder_id:
        return True, _("folder_create_success"), folder_id
    else:
        return False, _("folder_create_fail"), None


async def _create_super_group(
    client: Client, user_id: int, name: str, desc: str, folder_id: Optional[int] = None
) -> Chat:
    """
    Create a super group with given name and desc.
    The group is a forum.

    Args:
        client: The aiotdlib client instance.
        name: Group title. Should be email alias
        desc: Group description. Should be email address
        folder_id: The folder the group should be in
        user_id: admin user id

    Returns:
        The group as Chat object.
    """
    group = await client.api.create_new_supergroup_chat(
        title=name, is_forum=True, description=desc, message_auto_delete_time=0
    )
    if folder_id:
        folder = await client.api.get_chat_folder(folder_id)
        folder.included_chat_ids.append(group.id)
        await client.api.edit_chat_folder(chat_folder_id=folder_id, folder=folder)
    bot_id = await client.get_my_id()
    logger.debug(f"bot id is {bot_id}")
    await client.api.add_chat_members(chat_id=group.id, user_ids=[bot_id, user_id])
    return group


async def get_group_id(
    client: Client,
    user_id: int,
    email: str,
    folder_id: Optional[int] = None,
    alias: Optional[str] = None,
) -> tuple[bool, str, Optional[int]]:
    """
    Get group id of the email account. If not exists, create one.

    Args:
        client: The aiotdlib client instance.
        folder_id: the folder the group should be in
        email: email address
        alias: email acocunt alias
        user_id: admin user id

    Returns:
        Tuple (success: bool, message: str, group_id: Optional[int])
        group_id is the ID if creation was successful, otherwise None.
    """

    group_id = None
    group = None

    # get group id from data/group.txt
    group_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "group.txt")
    if os.path.exists(group_file_path):
        with open(group_file_path, "r") as f:
            groups = json.load(f)
            if email in groups:
                group_id = groups[email]

    if group_id and group_id > 0:
        try:
            group = await client.api.get_chat(group_id)
        except Exception as e:
            logger.error(f"Failed to get group {group_id}: {e}")
            group = None

    if not group:
        group = await _create_super_group(
            client=client, user_id=user_id, name=alias, desc=email
        )
        if group:
            group_id = group.id
            groups[email] = group_id
            with open(group_file_path, "w+") as f:
                json.dump(groups, f)

    if group_id:
        return True, _("group_create_success"), group_id
    else:
        return False, _("group_create_fail"), None
