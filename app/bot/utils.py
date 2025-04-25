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
from app.data import DataManager

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


async def _ensure_groups_in_folder(folder_id: int):
    groups = DataManager().get_groups()
    if groups is None:
        return
    client = UserClient().client
    folder = await client.api.get_chat_folder(chat_folder_id=folder_id)
    edited_folder = folder.model_copy()
    groups_in_folder = edited_folder.included_chat_ids
    for email, group_id in groups.items():
        if group_id not in groups_in_folder:
            groups_in_folder.append(group_id)
    bot_info = await BotClient().client.api.get_me()
    bot_id = bot_info.id
    if bot_id not in groups_in_folder:
        groups_in_folder.append(bot_id)
    try:
        logger.debug(f"editing existing folder: {edited_folder}")
        await client.api.edit_chat_folder(
            chat_folder_id=folder_id, folder=edited_folder
        )
    except Exception as e:
        logger.error(f"Failed to edit chat folder: {e}")


async def _create_email_folder(
    folder_name: str, icon_name: str = "Work"
) -> Optional[int]:
    try:
        groups = DataManager().get_groups()
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
        return folder_info.id
    except Exception as e:
        logger.error(f"Failed to create chat folder '{folder_name}': {e}")
        return None


async def get_email_folder_id() -> tuple[bool, str]:
    """
    Get folder id of "Email". If not exists, create one.

    Returns:
        Returns:
        Tuple (success: bool, message: str)
    """
    folder_name = "Email"
    folder_id = DataManager().get_folder_id()
    logger.debug(folder_id)

    if folder_id:
        try:
            await _ensure_groups_in_folder(folder_id=folder_id)
        except Exception as e:
            logger.error(f"failed to add all groups to email folder: {e}")
    else:
        folder_id = await _create_email_folder(folder_name=folder_name)
        if folder_id:
            await _ensure_groups_in_folder(folder_id=folder_id)
            DataManager().save_folder_id(folder_id=folder_id)

    if folder_id:
        return True, _("folder_create_success")
    else:
        return False, _("folder_create_fail")


async def _create_super_group(name: str, desc: str, provider_name: str = None) -> Chat:
    """
    Create a super group with given name and desc.
    The group is a forum.

    Args:
        name: Group title. Should be email alias
        desc: Group description. Should be email address
        provider_name: Email provider name for setting appropriate icon

    Returns:
        The group as Chat object.
    """
    client = UserClient().client
    group = await client.api.create_new_supergroup_chat(
        title=name, is_forum=True, description=desc, message_auto_delete_time=0
    )

    # Initialize photo path
    photo_path = None

    # Check if provider name is provided
    if provider_name:
        # Map provider names to icon filenames
        provider_icons = {
            "Gmail": "gmail.jpg",
            "Outlook/Hotmail": "outlook.jpg",
            "Yahoo Mail": "yahoo.jpg",
            "ProtonMail": "proton.jpg",
            "iCloud": "icloud.jpg",
            "Zoho Mail": "zoho.jpg",
            "QQ Mail": "qq.jpg",
            "NetEase (163/126)": "163.jpg",
            "Tencent Exmail": "wework.jpg",
            "Alibaba Mail": "alimail.jpg",
            "Yandex": "yandex.jpg",
        }

        # Get the icon filename if available for this provider
        icon_filename = provider_icons.get(provider_name)
        if icon_filename:
            photo_path = os.path.join(
                os.getcwd(), "app", "resources", "icons", icon_filename
            )
            if not os.path.exists(photo_path):
                logger.debug(
                    f"Icon file {photo_path} not found for provider {provider_name}"
                )
                photo_path = None

    # Set the photo if we found one
    if photo_path and os.path.exists(photo_path):
        try:
            await client.api.set_chat_photo(
                chat_id=group.id,
                photo=InputChatPhotoStatic(photo=InputFileLocal(path=photo_path)),
            )
            logger.debug(f"Set group icon to {photo_path} for {desc}")
        except Exception as e:
            logger.error(f"Failed to set chat photo: {e}")

    # Add bot to the group
    bot_id = await BotClient().client.get_my_id()
    await client.api.add_chat_members(chat_id=group.id, user_ids=[bot_id])
    return group


async def get_group_id(
    email: str,
    alias: Optional[str] = None,
    provider_name: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Get group id of the email account. If not exists, create one.

    Args:
        email: email address
        alias: email account alias
        provider_name: Provider name for icon selection

    Returns:
        Tuple (success: bool, message: str)
    """

    group_id = None
    group = None
    groups = DataManager().get_groups()
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
        group = await _create_super_group(
            name=alias, desc=email, provider_name=provider_name
        )
        if group:
            group_id = group.id
            groups[email] = group_id
            DataManager().save_groups(groups)

    if group_id:
        return True, _("group_create_success")
    else:
        return False, _("group_create_fail")
