import re
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
    LinkPreviewOptions,
)

from app.bot.conversation import Conversation
from app.bot.handlers.access import validate_admin
from app.bot.handlers.draft_contacts import list_draft_contacts
from app.bot.handlers.draft_recipient_picker import (
    build_recipient_picker_rows,
    build_recipient_picker_session,
    set_recipient_picker_session,
)
from app.database import DBManager
from app.email_utils.account_manager import AccountManager
from app.email_utils.signatures import (
    format_signature_choice_label,
    get_account_last_signature_choice,
    get_draft_signature_choice,
    normalize_signature_choice,
    set_draft_signature_choice,
)
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)

_EMAIL_RE = re.compile(r"^[\w\.-]+@[\w\.-]+\.\w+$")


def _split_recipients(value: str) -> list[str]:
    seen: set[str] = set()
    recipients: list[str] = []
    for part in (value or "").split(","):
        email = (part or "").strip()
        if not email:
            continue
        lowered = email.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        recipients.append(email)
    return recipients


def _normalize_recipients(value: str) -> str:
    return ",".join(_split_recipients(value))


def _validate_recipients_required(value: str) -> tuple[bool, str]:
    recipients = _split_recipients(value)
    if not recipients:
        return False, _("compose_invalid_recipients_required")
    if not all(_EMAIL_RE.match(email) for email in recipients):
        return False, _("compose_invalid_recipients")
    return True, ""


def _validate_recipients_optional(value: str) -> tuple[bool, str]:
    recipients = _split_recipients(value)
    if not recipients:
        return True, ""
    if not all(_EMAIL_RE.match(email) for email in recipients):
        return False, _("compose_invalid_recipients")
    return True, ""


def _build_recipient_reply_markup(
    *, context: dict, field: str, optional: bool
) -> ReplyMarkupInlineKeyboard | None:
    account_id = int(context.get("account_id") or 0)
    draft_id = int(context.get("draft_id") or 0)
    chat_id = int(context.get("chat_id") or 0)
    user_id = int(context.get("user_id") or 0)
    thread_id = int(context.get("message_thread_id") or 0)
    if account_id <= 0 or draft_id <= 0 or chat_id == 0 or user_id <= 0:
        return None

    db = DBManager()
    contacts = list_draft_contacts(
        db=db,
        account_id=account_id,
        query="",
        limit=20,
    )
    if not contacts:
        return None

    draft = None
    if thread_id > 0:
        draft = db.get_active_draft(chat_id=chat_id, thread_id=thread_id)

    if not draft or int(draft.get("id") or 0) != draft_id:
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, to_addrs, cc_addrs, bcc_addrs FROM drafts WHERE id = ? AND status = 'open'",
            (draft_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        draft = {
            "id": int(row[0]),
            "to_addrs": row[1] or "",
            "cc_addrs": row[2] or "",
            "bcc_addrs": row[3] or "",
        }

    session = build_recipient_picker_session(
        draft=draft,
        field=field,
        contacts=contacts,
        query="",
        include_cancel=False,
        include_skip=bool(optional),
    )
    rows = build_recipient_picker_rows(
        draft_id=draft_id,
        field=field,
        session=session,
    )
    if not rows:
        return None

    set_recipient_picker_session(
        chat_id=chat_id,
        user_id=user_id,
        draft_id=draft_id,
        field=field,
        session=session,
    )

    return ReplyMarkupInlineKeyboard(rows=rows)


def _build_compose_steps() -> list[dict]:
    return [
        {
            "text": _("compose_input_to"),
            "key": "to_addrs",
            "validate": _validate_recipients_required,
            "process": _normalize_recipients,
            "reply_markup": lambda ctx: _build_recipient_reply_markup(
                context=ctx,
                field="to",
                optional=False,
            ),
        },
        {
            "text": f"{_('compose_input_cc')}\n{_('send_new_or_skip')}",
            "key": "cc_addrs",
            "optional": True,
            "validate": _validate_recipients_optional,
            "process": _normalize_recipients,
            "reply_markup": lambda ctx: _build_recipient_reply_markup(
                context=ctx,
                field="cc",
                optional=True,
            ),
        },
        {
            "text": f"{_('compose_input_bcc')}\n{_('send_new_or_skip')}",
            "key": "bcc_addrs",
            "optional": True,
            "validate": _validate_recipients_optional,
            "process": _normalize_recipients,
            "reply_markup": lambda ctx: _build_recipient_reply_markup(
                context=ctx,
                field="bcc",
                optional=True,
            ),
        },
        {
            "text": f"{_('compose_input_subject')}\n{_('send_new_or_skip')}",
            "key": "subject",
            "optional": True,
        },
        {
            "text": f"{_('compose_input_body')}\n{_('send_new_or_skip')}",
            "key": "body_markdown",
            "optional": True,
        },
    ]


def _build_draft_card_keyboard(*, draft_id: int) -> list[list[InlineKeyboardButton]]:
    return [
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


def _build_draft_card_text(*, draft: dict, signature_label: str, attachments_count: int) -> str:
    body = draft.get("body_markdown") or ""
    return (
        f"📝 {_('draft')}\n\n"
        f"From: {draft.get('from_identity_email') or ''}\n"
        f"To: {draft.get('to_addrs') or ''}\n"
        f"Cc: {draft.get('cc_addrs') or ''}\n"
        f"Bcc: {draft.get('bcc_addrs') or ''}\n"
        f"Subject: {draft.get('subject') or ''}\n"
        f"{_('draft_signature')}: {signature_label}\n"
        f"{_('draft_attachments')}: {attachments_count}\n"
        f"Body: {len(body)} chars\n\n"
        f"{_('draft_help_commands')}"
    )


async def _refresh_draft_card(*, client: Client, db: DBManager, chat_id: int, thread_id: int) -> None:
    draft = db.get_active_draft(chat_id=chat_id, thread_id=thread_id)
    if not draft:
        return

    card_message_id = draft.get("card_message_id")
    if not card_message_id:
        return

    account = db.get_account(id=draft["account_id"]) or {}
    signature_label = format_signature_choice_label(
        account.get("signature"),
        get_draft_signature_choice(draft_id=int(draft["id"])),
    )
    attachments = db.list_draft_attachments(draft_id=draft["id"])
    card_text = _build_draft_card_text(
        draft=draft,
        signature_label=signature_label,
        attachments_count=len(attachments),
    )

    try:
        await client.edit_text(
            chat_id=int(chat_id),
            message_id=int(card_message_id),
            text=card_text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            clear_draft=False,
            reply_markup=ReplyMarkupInlineKeyboard(
                rows=_build_draft_card_keyboard(draft_id=int(draft["id"]))
            ),
        )
    except Exception as e:
        logger.error(f"Failed to update compose draft card: {e}")


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

    card_text = _build_draft_card_text(
        draft={
            "from_identity_email": from_email,
            "to_addrs": "",
            "cc_addrs": "",
            "bcc_addrs": "",
            "subject": "",
            "body_markdown": "",
        },
        signature_label=signature_label,
        attachments_count=0,
    )

    card_message = await client.api.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        input_message_content=InputMessageText(
            text=FormattedText(text=card_text, entities=[])
        ),
        reply_markup=ReplyMarkupInlineKeyboard(
            rows=_build_draft_card_keyboard(draft_id=int(draft_id))
        ),
    )

    db.update_draft(draft_id=draft_id, updates={"card_message_id": card_message.id})

    # Try pinning the draft card for better UX.
    try:
        await client.api.pin_chat_message(
            chat_id=chat_id, message_id=card_message.id, disable_notification=True
        )
    except Exception as e:
        logger.debug(f"Failed to pin draft card message: {e}")

    conversation = await Conversation.create_conversation(
        client=client,
        chat_id=chat_id,
        user_id=update.message.sender_id.user_id,
        steps=_build_compose_steps(),
        context={
            "draft_id": int(draft_id),
            "account_id": int(account["id"]),
            "message_thread_id": int(thread_id),
            "callback_passthrough_prefixes": ["draft:", "email:"],
            "disable_default_cancel_button": True,
        },
    )

    async def on_complete(context: dict) -> None:
        updates = {}
        for key in ("to_addrs", "cc_addrs", "bcc_addrs", "subject", "body_markdown"):
            if key in context:
                updates[key] = context.get(key)
        if updates:
            db.update_draft(draft_id=int(context["draft_id"]), updates=updates)
            await _refresh_draft_card(
                client=client,
                db=db,
                chat_id=int(chat_id),
                thread_id=int(thread_id),
            )

    conversation.on_finish(on_complete)
    await conversation.start()
