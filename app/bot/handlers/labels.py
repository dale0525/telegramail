from __future__ import annotations

from aiotdlib import Client
from aiotdlib.api import FormattedText, InputMessageText, UpdateNewMessage

from app.bot.handlers.access import validate_admin
from app.bot.handlers.command_filters import parse_bot_command
from app.bot.handlers.labels_ui import (
    build_label_list_view,
    build_label_panel,
    build_label_stats_view,
    parse_days,
    resolve_chat_scope_account_ids,
)
from app.email_utils.labels import normalize_llm_category
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


def _extract_text(update: UpdateNewMessage) -> str:
    try:
        content = getattr(update.message, "content", None)
        formatted = getattr(content, "text", None)
        text = getattr(formatted, "text", None)
        return text if isinstance(text, str) else ""
    except Exception:
        return ""


async def _send_text_with_keyboard(
    *,
    client: Client,
    chat_id: int,
    text: str,
    reply_markup,
) -> None:
    await client.api.send_message(
        chat_id=chat_id,
        input_message_content=InputMessageText(text=FormattedText(text=text, entities=[])),
        reply_markup=reply_markup,
    )


async def label_command_handler(client: Client, update: UpdateNewMessage):
    """handle /label command"""
    if not validate_admin(update):
        return

    chat_id = update.message.chat_id
    account_ids = resolve_chat_scope_account_ids(chat_id=chat_id)
    if not account_ids:
        await client.send_text(chat_id, _("label_must_in_account_group"))
        return

    text = _extract_text(update)
    _cmd, _mention, args = parse_bot_command(text)

    if not args:
        panel_text, panel_markup = build_label_panel()
        await _send_text_with_keyboard(
            client=client,
            chat_id=chat_id,
            text=panel_text,
            reply_markup=panel_markup,
        )
        return

    keyword = str(args[0]).strip().lower()
    if keyword in {"stats", "统计"}:
        days = parse_days(args[1] if len(args) > 1 else None)
        stats_text, stats_markup = build_label_stats_view(
            days=days, account_ids=account_ids
        )
        await _send_text_with_keyboard(
            client=client,
            chat_id=chat_id,
            text=stats_text,
            reply_markup=stats_markup,
        )
        return

    category = normalize_llm_category(keyword)
    days = parse_days(args[1] if len(args) > 1 else None)
    list_text, list_markup = build_label_list_view(
        category=category,
        days=days,
        offset=0,
        account_ids=account_ids,
    )
    await _send_text_with_keyboard(
        client=client,
        chat_id=chat_id,
        text=list_text,
        reply_markup=list_markup,
    )
