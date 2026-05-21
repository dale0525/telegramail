from typing import Any, Optional

from app.utils import Logger
from app.utils.telegram_errors import is_chat_not_found_error

logger = Logger().get_logger(__name__)


async def _is_delivery_chat_available(bot_client: Any, chat_id: int) -> bool:
    try:
        await bot_client.api.get_chat(chat_id=int(chat_id))
        return True
    except Exception as e:
        if is_chat_not_found_error(e):
            logger.warning(
                f"Configured Telegram group {chat_id} is not available to the bot",
                exc_info=False,
            )
            return False
        logger.warning(
            f"Could not verify Telegram group {chat_id}; continuing with configured group: {e}",
            exc_info=False,
        )
        return True


async def ensure_account_delivery_group(
    account: dict[str, Any], account_manager: Any, bot_client: Any
) -> Optional[int]:
    account_id = int(account["id"])
    group_id = account.get("tg_group_id")
    try:
        group_id = int(group_id) if group_id else None
    except (TypeError, ValueError):
        logger.warning(
            f"Invalid Telegram group ID configured for account {account_id}: {group_id}",
            exc_info=False,
        )
        group_id = None

    if group_id and await _is_delivery_chat_available(bot_client, group_id):
        return group_id

    from app.bot.utils import _create_super_group

    group_name = (
        str(account.get("alias") or account.get("email") or "Email").strip()
        or "Email"
    )
    group_desc = str(account.get("email") or group_name)
    group = await _create_super_group(name=group_name, desc=group_desc)
    new_group_id = getattr(group, "id", None)
    if not new_group_id:
        logger.error(
            f"Failed to create replacement Telegram group for account {account_id}"
        )
        return None

    new_group_id = int(new_group_id)
    if not account_manager.update_account({"tg_group_id": new_group_id}, id=account_id):
        logger.error(
            f"Failed to persist replacement Telegram group {new_group_id} for account {account_id}"
        )
    else:
        logger.info(
            f"Replaced Telegram group for account {account_id}: {group_id} -> {new_group_id}"
        )
    return new_group_id
