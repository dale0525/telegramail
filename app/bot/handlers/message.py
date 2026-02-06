import time

from aiotdlib import Client
from aiotdlib.api import UpdateNewMessage
from app.bot.conversation import Conversation
from app.bot.handlers.draft_contacts import list_draft_contacts
from app.bot.handlers.draft_recipient_picker import (
    build_recipient_picker_rows,
    build_recipient_picker_session,
    build_recipient_picker_text,
    set_recipient_picker_session,
)
from app.bot.handlers.access import validate_admin
from app.bot.handlers.command_filters import parse_bot_command
from app.utils import Logger
from app.database import DBManager
from app.email_utils.signatures import (
    CHOICE_DEFAULT,
    CHOICE_NONE,
    format_signature_choice_label,
    get_draft_signature_choice,
    list_account_signatures,
    set_draft_signature_choice,
)
from app.i18n import _
from aiotdlib.api import (
    InlineKeyboardButton,
    ReplyMarkupInlineKeyboard,
    InlineKeyboardButtonTypeCallback,
    LinkPreviewOptions,
    InputMessageText,
    FormattedText,
)

logger = Logger().get_logger(__name__)


# handle non-command messages
async def message_handler(client: Client, update: UpdateNewMessage):
    """handle all non-command messages and route them to active conversations (if exists)"""
    logger.debug(f"receive message: {update}")
    if not validate_admin(update):
        return

    content = update.message.content
    if not content or not hasattr(content, "ID"):
        return

    chat_id = update.message.chat_id
    user_id = update.message.sender_id.user_id

    # check if there's any active conversations
    if content.ID == "messageText":
        conversation = Conversation.get_instance(chat_id, user_id)
        if conversation:
            handled = await conversation.handle_update(update)
            if handled:
                return

    # Draft topic editing (compose/reply/forward)
    thread_id = getattr(update.message, "message_thread_id", 0) or 0
    if thread_id <= 0:
        return

    db = DBManager()
    draft = db.get_active_draft(chat_id=chat_id, thread_id=thread_id)
    if not draft:
        return

    content = update.message.content
    if not content or not hasattr(content, "ID"):
        return

    text = ""
    if content.ID == "messageText":
        text = (content.text.text or "").strip()
        if not text:
            return

        # Track user text messages created while editing a draft so we can clean them up
        # after the draft is sent (keep the topic tidy).
        try:
            msg_id = getattr(update.message, "id", None)
            if msg_id:
                db.record_draft_message(
                    draft_id=int(draft["id"]),
                    chat_id=int(chat_id),
                    thread_id=int(thread_id),
                    message_id=int(msg_id),
                    message_type="text",
                )
        except Exception:
            pass
        cmd, _mentioned_bot, args = parse_bot_command(text)
        cmd_arg = " ".join(args).strip() if args else ""
        updates = None

        async def _send_draft_help() -> None:
            try:
                await client.api.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    input_message_content=InputMessageText(
                        text=FormattedText(text=_("draft_help_commands"), entities=[])
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to send draft help message: {e}")

        async def _send_recipient_selector(field_cmd: str, query: str = "") -> None:
            field = (field_cmd or "").strip().lower()
            if field not in {"to", "cc", "bcc"}:
                await _send_draft_help()
                return

            contacts = list_draft_contacts(
                db=db,
                account_id=int(draft["account_id"]),
                query=(query or "").strip(),
                limit=20,
            )

            if not contacts:
                try:
                    await client.api.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        input_message_content=InputMessageText(
                            text=FormattedText(text=_("draft_contacts_empty"), entities=[])
                        ),
                    )
                except Exception as e:
                    logger.error(f"Failed to send empty-contact message: {e}")
                return

            filter_text = (query or "").strip()

            session = build_recipient_picker_session(
                draft=draft,
                field=field,
                contacts=contacts,
                query=filter_text,
            )
            rows = build_recipient_picker_rows(
                draft_id=int(draft["id"]),
                field=field,
                session=session,
            )
            if not rows:
                await _send_draft_help()
                return

            set_recipient_picker_session(
                chat_id=int(chat_id),
                user_id=int(user_id),
                draft_id=int(draft["id"]),
                field=field,
                session=session,
            )

            try:
                await client.api.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    input_message_content=InputMessageText(
                        text=FormattedText(
                            text=build_recipient_picker_text(field=field, session=session),
                            entities=[],
                        )
                    ),
                    reply_markup=ReplyMarkupInlineKeyboard(rows=rows),
                )
            except Exception as e:
                logger.error(f"Failed to send recipient selector: {e}")

        def _ensure_identities():
            identities = db.list_account_identities(account_id=draft["account_id"])
            if identities:
                return identities

            account = db.get_account(id=draft["account_id"])
            if not account:
                return identities

            account_email = (account.get("email") or "").strip().lower()
            if not account_email:
                return identities

            db.upsert_account_identity(
                account_id=int(draft["account_id"]),
                from_email=account_email,
                display_name=account.get("alias") or account_email,
                is_default=True,
            )
            return db.list_account_identities(account_id=draft["account_id"])

        if cmd == "subject" and not args:
            await _send_draft_help()
            return

        # Manage attachments
        if cmd in {"attachments", "attach"} and not args:
            attachments = db.list_draft_attachments(draft_id=draft["id"])
            if not attachments:
                try:
                    await client.api.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        input_message_content=InputMessageText(
                            text=FormattedText(text=_("draft_no_attachments"), entities=[])
                        ),
                    )
                except Exception as e:
                    logger.error(f"Failed to send no-attachments message: {e}")
                return

            rows = []
            for att in attachments[:40]:
                label = att.get("file_name") or f"#{att.get('id')}"
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"🗑 {label}",
                            type=InlineKeyboardButtonTypeCallback(
                                data=f"draft:att:rm:{draft['id']}:{att['id']}".encode(
                                    "utf-8"
                                )
                            ),
                        )
                    ]
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🧹 {_('clear_all')}",
                        type=InlineKeyboardButtonTypeCallback(
                            data=f"draft:att:clear:{draft['id']}".encode("utf-8")
                        ),
                    )
                ]
            )

            try:
                await client.api.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    input_message_content=InputMessageText(
                        text=FormattedText(text=_("draft_attachments_manage"), entities=[])
                    ),
                    reply_markup=ReplyMarkupInlineKeyboard(rows=rows),
                )
            except Exception as e:
                logger.error(f"Failed to send attachments manager: {e}")
            return

        # Choose from identity
        if cmd == "from" and not args:
            identities = _ensure_identities()
            rows = []
            for identity in identities:
                if int(identity.get("enabled") or 0) != 1:
                    continue
                label = identity.get("from_email") or ""
                display_name = (identity.get("display_name") or "").strip()
                if display_name and display_name not in label:
                    label = f"{display_name} <{label}>"
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=label,
                            type=InlineKeyboardButtonTypeCallback(
                                data=f"draft:set_from:{draft['id']}:{identity['id']}".encode(
                                    "utf-8"
                                )
                            ),
                        )
                    ]
                )

            if not rows:
                await _send_draft_help()
                return

            try:
                await client.api.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    input_message_content=InputMessageText(
                        text=FormattedText(text=_("draft_choose_from"), entities=[])
                    ),
                    reply_markup=ReplyMarkupInlineKeyboard(rows=rows),
                )
            except Exception as e:
                logger.error(f"Failed to send from-identity selector: {e}")
            return

        if cmd in {"signature", "sig"} and not args:
            account = db.get_account(id=draft["account_id"]) or {}
            items, _default_id = list_account_signatures(account.get("signature"))
            if not items:
                await _send_draft_help()
                return

            rows = [
                [
                    InlineKeyboardButton(
                        text=f"⭐ {_('signature_use_default')}",
                        type=InlineKeyboardButtonTypeCallback(
                            data=f"draft:set_sig:{draft['id']}:{CHOICE_DEFAULT}".encode(
                                "utf-8"
                            )
                        ),
                    ),
                    InlineKeyboardButton(
                        text=f"🚫 {_('signature_disable')}",
                        type=InlineKeyboardButtonTypeCallback(
                            data=f"draft:set_sig:{draft['id']}:{CHOICE_NONE}".encode(
                                "utf-8"
                            )
                        ),
                    ),
                ]
            ]
            for item in items[:20]:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"✍️ {item.get('name') or item.get('id')}",
                            type=InlineKeyboardButtonTypeCallback(
                                data=f"draft:set_sig:{draft['id']}:{item['id']}".encode(
                                    "utf-8"
                                )
                            ),
                        )
                    ]
                )

            try:
                await client.api.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    input_message_content=InputMessageText(
                        text=FormattedText(
                            text=_("draft_choose_signature"),
                            entities=[],
                        )
                    ),
                    reply_markup=ReplyMarkupInlineKeyboard(rows=rows),
                )
            except Exception as e:
                logger.error(f"Failed to send signature selector: {e}")
            return

        if cmd in {"to", "cc", "bcc"}:
            if "@" in cmd_arg:
                field_map = {
                    "to": "to_addrs",
                    "cc": "cc_addrs",
                    "bcc": "bcc_addrs",
                }
                updates = {field_map[cmd]: cmd_arg}
            else:
                await _send_recipient_selector(field_cmd=cmd, query=cmd_arg)
                return
        elif cmd == "from" and args:
            requested = cmd_arg.strip()
            identities = _ensure_identities()
            match = None
            for identity in identities:
                addr = (identity.get("from_email") or "").strip().lower()
                if addr and addr == requested.lower():
                    match = identity
                    break
            if not match:
                await _send_draft_help()
                return

            updates = {"from_identity_email": match["from_email"]}
        elif cmd == "subject" and args:
            updates = {"subject": cmd_arg}
        elif cmd in {"signature", "sig"} and args:
            account = db.get_account(id=draft["account_id"]) or {}
            items, _default_id = list_account_signatures(account.get("signature"))
            if not items:
                await _send_draft_help()
                return

            requested = cmd_arg.strip()
            lower = requested.lower()
            choice = None
            if lower in {"none", "off", "disable"}:
                choice = CHOICE_NONE
            elif lower in {"default"}:
                choice = CHOICE_DEFAULT
            elif requested.isdigit():
                idx = int(requested) - 1
                if 0 <= idx < len(items):
                    choice = items[idx]["id"]
            else:
                for item in items:
                    if lower in {
                        str(item.get("id") or "").lower(),
                        str(item.get("name") or "").lower(),
                    }:
                        choice = item["id"]
                        break

            if not choice:
                await _send_draft_help()
                return
            set_draft_signature_choice(draft_id=int(draft["id"]), choice=choice)
            updates = {}

        if updates is not None:
            db.update_draft(draft_id=draft["id"], updates=updates)
        else:
            # Treat as body Markdown and append
            db.append_draft_body(draft_id=draft["id"], text=text)

    else:
        file_id = None
        remote_id = None
        file_name = None
        mime_type = None
        size = None
        file_type = None

        try:
            if content.ID == "messageDocument":
                doc = content.document
                file_name = getattr(doc, "file_name", None)
                mime_type = getattr(doc, "mime_type", None)
                f = getattr(doc, "document", None)
                file_id = getattr(f, "id", None)
                size = getattr(f, "size", None) or getattr(f, "expected_size", None)
                remote = getattr(f, "remote", None)
                remote_id = getattr(remote, "id", None) if remote else None
                file_type = "document"
            elif content.ID == "messagePhoto":
                photo = content.photo
                sizes = list(getattr(photo, "sizes", []) or [])
                best = sizes[-1] if sizes else None
                f = getattr(best, "photo", None) if best else None
                file_id = getattr(f, "id", None)
                size = getattr(f, "size", None) or getattr(f, "expected_size", None)
                remote = getattr(f, "remote", None)
                remote_id = getattr(remote, "id", None) if remote else None
                mime_type = "image/jpeg"
                msg_id = getattr(update.message, "id", None) or int(time.time())
                file_name = f"photo_{msg_id}.jpg"
                file_type = "photo"
            elif content.ID == "messageVideo":
                video = content.video
                file_name = getattr(video, "file_name", None) or "video.mp4"
                mime_type = getattr(video, "mime_type", None) or "video/mp4"
                f = getattr(video, "video", None)
                file_id = getattr(f, "id", None)
                size = getattr(f, "size", None) or getattr(f, "expected_size", None)
                remote = getattr(f, "remote", None)
                remote_id = getattr(remote, "id", None) if remote else None
                file_type = "video"
            elif content.ID == "messageAudio":
                audio = content.audio
                file_name = getattr(audio, "file_name", None) or "audio"
                mime_type = getattr(audio, "mime_type", None) or "audio/mpeg"
                f = getattr(audio, "audio", None)
                file_id = getattr(f, "id", None)
                size = getattr(f, "size", None) or getattr(f, "expected_size", None)
                remote = getattr(f, "remote", None)
                remote_id = getattr(remote, "id", None) if remote else None
                file_type = "audio"
            elif content.ID == "messageAnimation":
                animation = content.animation
                file_name = getattr(animation, "file_name", None) or "animation.gif"
                mime_type = getattr(animation, "mime_type", None) or "image/gif"
                f = getattr(animation, "animation", None)
                file_id = getattr(f, "id", None)
                size = getattr(f, "size", None) or getattr(f, "expected_size", None)
                remote = getattr(f, "remote", None)
                remote_id = getattr(remote, "id", None) if remote else None
                file_type = "animation"
            elif content.ID == "messageVoiceNote":
                voice_note = content.voice_note
                mime_type = getattr(voice_note, "mime_type", None) or "audio/ogg"
                msg_id = getattr(update.message, "id", None) or int(time.time())
                file_name = f"voice_{msg_id}.ogg"
                f = getattr(voice_note, "voice", None)
                file_id = getattr(f, "id", None)
                size = getattr(f, "size", None) or getattr(f, "expected_size", None)
                remote = getattr(f, "remote", None)
                remote_id = getattr(remote, "id", None) if remote else None
                file_type = "voice_note"
            else:
                return
        except Exception as e:
            logger.error(f"Failed to parse draft attachment content: {e}")
            return

        if not file_id or not file_name:
            return

        # Track user attachment messages created while editing a draft so we can clean them up
        # after the draft is sent (keep the topic tidy).
        try:
            msg_id = getattr(update.message, "id", None)
            if msg_id:
                db.record_draft_message(
                    draft_id=int(draft["id"]),
                    chat_id=int(chat_id),
                    thread_id=int(thread_id),
                    message_id=int(msg_id),
                    message_type=(file_type or "").strip() or "attachment",
                )
        except Exception:
            pass

        db.add_draft_attachment(
            draft_id=draft["id"],
            file_id=int(file_id),
            remote_id=remote_id,
            file_type=file_type,
            file_name=str(file_name),
            mime_type=mime_type,
            size=int(size) if size is not None else None,
        )

        # Append caption (if any) to body.
        try:
            caption = getattr(content, "caption", None)
            caption_text = (getattr(caption, "text", "") or "").strip()
        except Exception:
            caption_text = ""
        if caption_text:
            db.append_draft_body(draft_id=draft["id"], text=caption_text)

    # Refresh card message if we have one
    refreshed = db.get_active_draft(chat_id=chat_id, thread_id=thread_id)
    if not refreshed:
        return

    card_message_id = refreshed.get("card_message_id")
    if not card_message_id:
        return

    from_email = refreshed.get("from_identity_email") or ""
    account = db.get_account(id=refreshed["account_id"]) or {}
    sig_label = format_signature_choice_label(
        account.get("signature"),
        get_draft_signature_choice(draft_id=int(refreshed["id"])),
    )

    body = refreshed.get("body_markdown") or ""
    attachments = db.list_draft_attachments(draft_id=refreshed["id"])
    card_text = (
        f"📝 {_('draft')}\n\n"
        f"From: {from_email}\n"
        f"To: {refreshed.get('to_addrs') or ''}\n"
        f"Cc: {refreshed.get('cc_addrs') or ''}\n"
        f"Bcc: {refreshed.get('bcc_addrs') or ''}\n"
        f"Subject: {refreshed.get('subject') or ''}\n"
        f"{_('draft_signature')}: {sig_label}\n"
        f"{_('draft_attachments')}: {len(attachments)}\n"
        f"Body: {len(body)} chars\n\n"
        f"{_('draft_help_commands')}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                text=f"📤 {_('send')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:send:{refreshed['id']}".encode("utf-8")
                ),
            ),
            InlineKeyboardButton(
                text=f"❌ {_('cancel')}",
                type=InlineKeyboardButtonTypeCallback(
                    data=f"draft:cancel:{refreshed['id']}".encode("utf-8")
                ),
            ),
        ]
    ]

    try:
        await client.edit_text(
            chat_id=chat_id,
            message_id=int(card_message_id),
            text=card_text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            clear_draft=False,
            reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
        )
    except Exception as e:
        logger.error(f"Failed to update draft card: {e}")
