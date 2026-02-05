import time

from aiotdlib import Client
from aiotdlib.api import UpdateNewMessage
from app.bot.conversation import Conversation
from app.bot.handlers.access import validate_admin
from app.utils import Logger
from app.database import DBManager
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

        def _is_cmd(prefix: str) -> bool:
            return text.lower().startswith(prefix)

        def _cmd_arg(prefix: str) -> str:
            return text[len(prefix) :].strip()

        updates = None

        # Manage attachments
        if text.lower() in {"/attachments", "/attach"}:
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
        if text.lower() == "/from":
            identities = db.list_account_identities(account_id=draft["account_id"])
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

            if rows:
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

        if _is_cmd("/to "):
            updates = {"to_addrs": _cmd_arg("/to ")}
        elif _is_cmd("/cc "):
            updates = {"cc_addrs": _cmd_arg("/cc ")}
        elif _is_cmd("/bcc "):
            updates = {"bcc_addrs": _cmd_arg("/bcc ")}
        elif _is_cmd("/from "):
            requested = _cmd_arg("/from ").strip()
            identities = db.list_account_identities(account_id=draft["account_id"])
            match = None
            for identity in identities:
                addr = (identity.get("from_email") or "").strip().lower()
                if addr and addr == requested.lower():
                    match = identity
                    break
            if match:
                updates = {"from_identity_email": match["from_email"]}
        elif _is_cmd("/subject "):
            updates = {"subject": _cmd_arg("/subject ")}

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

    body = refreshed.get("body_markdown") or ""
    attachments = db.list_draft_attachments(draft_id=refreshed["id"])
    card_text = (
        f"📝 {_('draft')}\n\n"
        f"From: {from_email}\n"
        f"To: {refreshed.get('to_addrs') or ''}\n"
        f"Cc: {refreshed.get('cc_addrs') or ''}\n"
        f"Bcc: {refreshed.get('bcc_addrs') or ''}\n"
        f"Subject: {refreshed.get('subject') or ''}\n"
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
