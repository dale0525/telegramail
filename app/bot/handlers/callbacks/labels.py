from __future__ import annotations

from aiotdlib import Client
from aiotdlib.api import FormattedText, InputMessageText, UpdateNewCallbackQuery

from app.bot.handlers.labels_ui import (
    build_label_list_view,
    build_label_panel,
    build_label_stats_view,
    parse_days,
    parse_label_list_callback,
    resolve_chat_scope_account_ids,
)
from app.bot.utils import answer_callback
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def handle_label_callback(
    *, client: Client, update: UpdateNewCallbackQuery, data: str
) -> bool:
    chat_id = update.chat_id
    message_id = update.message_id
    account_ids = resolve_chat_scope_account_ids(chat_id=chat_id)

    if data == "label:panel":
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'label:panel' callback query: {e}")

        text, markup = build_label_panel()
        await client.edit_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            clear_draft=False,
            reply_markup=markup,
        )
        return True

    parsed_list = parse_label_list_callback(data)
    if parsed_list is not None:
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'label:list' callback query: {e}")

        text, markup = build_label_list_view(
            category=parsed_list["category"],
            days=parsed_list["days"],
            offset=parsed_list["offset"],
            account_ids=account_ids,
        )
        await client.edit_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            clear_draft=False,
            reply_markup=markup,
        )
        return True

    if data.startswith("label:stats:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'label:stats' callback query: {e}")

        parts = data.split(":", 2)
        days = parse_days(parts[2] if len(parts) > 2 else None)
        text, markup = build_label_stats_view(days=days, account_ids=account_ids)
        await client.edit_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            clear_draft=False,
            reply_markup=markup,
        )
        return True

    if data.startswith("label:locate:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'label:locate' callback query: {e}")

        parts = data.split(":", 2)
        try:
            thread_id = int(parts[2])
        except Exception:
            logger.warning(f"Invalid thread id in label locate callback: {data}")
            return True

        await client.api.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            input_message_content=InputMessageText(
                text=FormattedText(
                    text=f"📌 {_('label_locate_anchor')}",
                    entities=[],
                )
            ),
        )
        return True

    return False
