import time

from aiotdlib import Client
from aiotdlib.api import (
    UpdateNewMessage,
    ForumTopicIcon,
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    ReplyMarkupInlineKeyboard,
    InputMessageText,
    FormattedText,
)

from app.bot.handlers.access import validate_admin
from app.database import DBManager
from app.email_utils.account_manager import AccountManager
from app.email_utils.signatures import (
    format_signature_choice_label,
    get_account_last_signature_choice,
    normalize_signature_choice,
    set_draft_signature_choice,
)
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def compose_command_handler(client: Client, update: UpdateNewMessage) -> None:
    """
    /compose

    Creates a new draft topic in the current account group.
    """
    if not validate_admin(update):
        return

    chat_id = update.message.chat_id

    # Resolve account by group id.
    account_manager = AccountManager()
    account = next(
        (
            a
            for a in account_manager.get_all_accounts()
            if int(a.get("tg_group_id") or 0) == int(chat_id)
        ),
        None,
    )
    if not account:
        await client.send_text(chat_id, _("compose_must_in_account_group"))
        return

    db = DBManager()
    identities = db.list_account_identities(account_id=account["id"])
    default_identity = next(
        (i for i in identities if int(i.get("is_default") or 0) == 1), None
    )
    from_email = (default_identity or {}).get("from_email") or account["email"]

    # Create draft topic
    title = f"📝 Draft: {time.strftime('%m-%d %H:%M')}"
    topic = await client.api.create_forum_topic(
        chat_id=chat_id,
        name=title,
        icon=ForumTopicIcon(color=0x6FB9F0, custom_emoji_id=5309984423003823246),
    )
    thread_id = topic.message_thread_id

    draft_id = db.create_draft(
        account_id=account["id"],
        chat_id=chat_id,
        thread_id=thread_id,
        draft_type="compose",
        from_identity_email=from_email,
    )
    signature_choice = normalize_signature_choice(
        account.get("signature"),
        get_account_last_signature_choice(account_id=int(account["id"])),
    )
    set_draft_signature_choice(draft_id=int(draft_id), choice=signature_choice)
    signature_label = format_signature_choice_label(
        account.get("signature"),
        signature_choice,
    )

    card_text = (
        f"📝 {_('draft')}\n\n"
        f"From: {from_email}\n"
        f"To: \n"
        f"Cc: \n"
        f"Bcc: \n"
        f"Subject: \n\n"
        f"{_('draft_signature')}: {signature_label}\n\n"
        f"{_('draft_help_commands')}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                text=f"📤 {_('send')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:send:{draft_id}".encode("utf-8")
                ),
            ),
            InlineKeyboardButton(
                text=f"❌ {_('cancel')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:cancel:{draft_id}".encode("utf-8")
                ),
            ),
        ]
    ]

    card_message = await client.api.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        input_message_content=InputMessageText(
            text=FormattedText(text=card_text, entities=[])
        ),
        reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
    )

    db.update_draft(draft_id=draft_id, updates={"card_message_id": card_message.id})

    # Try pinning the draft card for better UX.
    try:
        await client.api.pin_chat_message(
            chat_id=chat_id, message_id=card_message.id, disable_notification=True
        )
    except Exception as e:
        logger.debug(f"Failed to pin draft card message: {e}")
