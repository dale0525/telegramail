import asyncio
import os
from email.utils import formatdate, make_msgid

from aiotdlib import Client
from aiotdlib.api import (
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    LinkPreviewOptions,
    InputFileId,
    InputFileRemote,
    InputMessageDocument,
    InputMessageText,
    FormattedText,
    ReplyMarkupInlineKeyboard,
    TextParseModeHTML,
    UpdateNewCallbackQuery,
)

from app.bot.handlers.draft_contacts import (
    append_contact_email,
    list_draft_contacts,
    resolve_contact_email_by_token,
)
from app.bot.handlers.draft_recipient_picker import (
    build_recipient_picker_rows,
    build_recipient_picker_text,
    clear_recipient_picker_session,
    get_recipient_picker_session,
    get_recipient_target_field,
    merge_recipient_picker_selection,
)
from app.bot.utils import answer_callback
from app.database import DBManager
from app.email_utils.account_manager import AccountManager
from app.email_utils.markdown_render import render_markdown_to_html
from app.email_utils.signatures import (
    CHOICE_DEFAULT,
    CHOICE_NONE,
    clear_draft_signature_choice,
    format_signature_choice_label,
    get_draft_signature_choice,
    list_account_signatures,
    normalize_signature_choice,
    resolve_signature_for_send,
    resolve_signature_choice_to_store,
    set_account_last_signature_choice,
    set_draft_signature_choice,
)
from app.email_utils.smtp_client import SMTPClient
from app.i18n import _
from app.telegram_ui.email_cards import build_outgoing_email_card
from app.utils import Logger

logger = Logger().get_logger(__name__)

_DEFAULT_COMPOSE_DRAFT_DELETE_DELAY_SECONDS = 3.0


def _get_compose_draft_delete_delay_seconds() -> float:
    raw = os.getenv("TELEGRAMAIL_COMPOSE_DRAFT_DELETE_DELAY_SECONDS")
    if raw is None or raw == "":
        return _DEFAULT_COMPOSE_DRAFT_DELETE_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except Exception:
        return _DEFAULT_COMPOSE_DRAFT_DELETE_DELAY_SECONDS


def _append_signature_to_markdown(body_markdown: str | None, signature: str | None) -> str:
    body = str(body_markdown or "").rstrip()
    sig = str(signature or "").strip()
    if not sig:
        return body
    if not body:
        return f"-- \n{sig}"
    return f"{body}\n\n-- \n{sig}"


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


def _build_draft_card_text(
    *, draft: dict, attachments_count: int, signature_label: str
) -> str:
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


async def _delete_compose_draft_topic_after_delay(
    *, client: Client, chat_id: int, thread_id: int, delay_seconds: float
) -> None:
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await client.api.delete_forum_topic(
            chat_id=int(chat_id), message_thread_id=int(thread_id)
        )
    except Exception as e:
        logger.error(f"Failed to delete compose draft topic {thread_id}: {e}")


async def _render_recipient_picker(
    *,
    client: Client,
    chat_id: int,
    message_id: int,
    user_id: int,
    draft_id: int,
    field: str,
) -> None:
    session = get_recipient_picker_session(
        chat_id=int(chat_id),
        user_id=int(user_id),
        draft_id=int(draft_id),
        field=field,
    )
    if not session:
        try:
            await client.edit_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=f"❌ {_('conversation_expired_or_not_found')}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception:
            pass
        return

    try:
        await client.edit_text(
            chat_id=int(chat_id),
            message_id=int(message_id),
            text=build_recipient_picker_text(field=field, session=session),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            clear_draft=False,
            reply_markup=ReplyMarkupInlineKeyboard(
                rows=build_recipient_picker_rows(
                    draft_id=int(draft_id),
                    field=field,
                    session=session,
                )
            ),
        )
    except Exception as e:
        logger.error(f"Failed to render recipient picker: {e}")


async def _submit_picker_selection_to_active_conversation(
    *,
    chat_id: int,
    user_id: int,
    draft_id: int,
    draft_thread_id: int,
    field: str,
    value: str,
) -> tuple[bool, bool, str]:
    """
    Try to feed rcpt picker result into active compose conversation step.

    Returns:
        matched: whether this callback maps to the active conversation step
        advanced: whether conversation accepted input and moved forward
        stored_value: normalized value stored in conversation context
    """
    try:
        from app.bot.conversation import Conversation, ConversationState
    except Exception:
        return False, False, value

    conversation = Conversation.get_instance(int(chat_id), int(user_id))
    if not conversation or conversation.state != ConversationState.ACTIVE:
        return False, False, value

    expected_key = get_recipient_target_field(field)
    if not expected_key:
        return False, False, value

    context = conversation.get_context()
    if int(context.get("draft_id") or 0) != int(draft_id):
        return False, False, value
    if int(context.get("message_thread_id") or 0) != int(draft_thread_id or 0):
        return False, False, value

    if conversation.current_step >= len(conversation.steps):
        return False, False, value
    current_step = conversation.steps[conversation.current_step] or {}
    if str(current_step.get("key") or "") != expected_key:
        return False, False, value

    advanced = await conversation.submit_external_input(value)
    stored = str(conversation.get_context().get(expected_key) or value)
    return True, bool(advanced), stored


async def handle_draft_callback(
    *, client: Client, update: UpdateNewCallbackQuery, data: str
) -> bool:
    chat_id = update.chat_id
    message_id = update.message_id

    if data.startswith("draft:cancel:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:cancel' callback query: {e}")

        try:
            draft_id = int(data.split(":", 2)[2])
        except Exception:
            logger.warning(f"Invalid draft id in callback data: {data}")
            return True

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT chat_id, thread_id, draft_type FROM drafts WHERE id = ?", (draft_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            logger.warning(f"Draft not found for cancel: {draft_id}")
            return True

        draft_chat_id, draft_thread_id, draft_type = row
        db.update_draft(draft_id=draft_id, updates={"status": "cancelled"})
        clear_draft_signature_choice(draft_id=draft_id)

        if str(draft_type) == "compose" and int(draft_thread_id or 0) > 0:
            try:
                await client.api.delete_forum_topic(
                    chat_id=int(draft_chat_id), message_thread_id=int(draft_thread_id)
                )
                return True
            except Exception as e:
                logger.error(f"Failed to delete compose draft topic {draft_thread_id}: {e}")

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ {_('draft_cancelled')}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception as e:
            logger.error(f"Failed to edit message after draft cancel: {e}")
        return True

    if data.startswith("draft:set_sig:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:set_sig' callback query: {e}")

        try:
            _p = data.split(":", 3)
            draft_id = int(_p[2])
            choice = (_p[3] or "").strip()
        except Exception:
            logger.warning(f"Invalid draft:set_sig callback data: {data}")
            return True

        db = DBManager()
        draft = None
        account = None
        try:
            conn = db._get_connection()
            conn.row_factory = None
            cur = conn.cursor()
            cur.execute(
                "SELECT account_id, chat_id, thread_id, card_message_id, status FROM drafts WHERE id = ?",
                (draft_id,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                account_id, draft_chat_id, draft_thread_id, card_message_id, status = row
                if str(status) == "open":
                    draft = db.get_active_draft(
                        chat_id=int(draft_chat_id), thread_id=int(draft_thread_id)
                    )
                    account = db.get_account(id=int(account_id))
                else:
                    return True
            else:
                return True
        except Exception as e:
            logger.warning(f"Failed loading draft for signature selection: {e}")
            return True

        if not draft or not account:
            return True

        items, _default_id = list_account_signatures(account.get("signature"))
        valid_ids = {it["id"] for it in items}
        if choice in {CHOICE_NONE, CHOICE_DEFAULT} or choice in valid_ids:
            set_draft_signature_choice(draft_id=draft_id, choice=choice)

        card_message_id = draft.get("card_message_id")
        if card_message_id:
            sig_label = format_signature_choice_label(
                account.get("signature"),
                get_draft_signature_choice(draft_id=draft_id),
            )
            attachments = db.list_draft_attachments(draft_id=draft["id"])
            card_text = _build_draft_card_text(
                draft=draft,
                attachments_count=len(attachments),
                signature_label=sig_label,
            )
            try:
                await client.edit_text(
                    chat_id=int(draft["chat_id"]),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(
                        rows=_build_draft_card_keyboard(draft_id=int(draft["id"]))
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to update draft card after set_sig: {e}")

        try:
            current_label = format_signature_choice_label(
                account.get("signature"),
                get_draft_signature_choice(draft_id=draft_id),
            )
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ {_('draft_signature')}: {current_label}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception:
            pass
        return True

    if data.startswith("draft:set_from:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:set_from' callback query: {e}")

        try:
            _p = data.split(":", 3)
            draft_id = int(_p[2])
            identity_id = int(_p[3])
        except Exception:
            logger.warning(f"Invalid draft:set_from callback data: {data}")
            return True

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT account_id, chat_id, thread_id, card_message_id, status FROM drafts WHERE id = ?",
            (draft_id,),
        )
        draft_row = cur.fetchone()
        if not draft_row:
            conn.close()
            logger.warning(f"Draft not found for set_from: {draft_id}")
            return True
        account_id, draft_chat_id, draft_thread_id, card_message_id, status = draft_row

        cur.execute(
            "SELECT account_id, from_email, enabled FROM account_identities WHERE id = ?",
            (identity_id,),
        )
        ident_row = cur.fetchone()
        conn.close()
        if not ident_row:
            logger.warning(f"Identity not found for set_from: {identity_id}")
            return True

        ident_account_id, from_email, enabled = ident_row
        if str(status) != "open":
            logger.info(f"Draft {draft_id} is not open; status={status}")
            return True
        if int(ident_account_id) != int(account_id):
            logger.warning(
                f"Identity {identity_id} does not belong to draft account {account_id}"
            )
            return True
        if int(enabled or 0) != 1:
            logger.warning(f"Identity {identity_id} is disabled; skipping")
            return True

        db.update_draft(draft_id=draft_id, updates={"from_identity_email": from_email})

        refreshed = db.get_active_draft(
            chat_id=int(draft_chat_id), thread_id=int(draft_thread_id)
        )
        if refreshed and card_message_id:
            account = db.get_account(id=int(refreshed["account_id"]))
            sig_label = format_signature_choice_label(
                (account or {}).get("signature"),
                get_draft_signature_choice(draft_id=int(refreshed["id"])),
            )
            attachments = db.list_draft_attachments(draft_id=refreshed["id"])
            card_text = _build_draft_card_text(
                draft=refreshed,
                attachments_count=len(attachments),
                signature_label=sig_label,
            )
            try:
                await client.edit_text(
                    chat_id=int(draft_chat_id),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(
                        rows=_build_draft_card_keyboard(draft_id=int(refreshed["id"]))
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to update draft card after set_from: {e}")

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ {_('email_from')}: {from_email}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception:
            pass
        return True

    if data.startswith("draft:rcpt_pick:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:rcpt_pick' callback query: {e}")

        parts = data.split(":")
        if len(parts) < 5:
            logger.warning(f"Invalid draft:rcpt_pick callback data: {data}")
            return True

        action = (parts[2] or "").strip().lower()
        try:
            draft_id = int(parts[3])
        except Exception:
            logger.warning(f"Invalid draft id in draft:rcpt_pick callback data: {data}")
            return True
        field = (parts[4] or "").strip().lower()
        if field not in {"to", "cc", "bcc"}:
            logger.warning(f"Invalid recipient field in draft:rcpt_pick callback data: {data}")
            return True

        chat_id = int(update.chat_id)
        user_id = int(update.sender_user_id)
        message_id = int(update.message_id)

        if action == "toggle":
            if len(parts) < 6:
                logger.warning(f"Missing index in draft:rcpt_pick:toggle callback data: {data}")
                return True
            try:
                idx = int(parts[5])
            except Exception:
                logger.warning(f"Invalid index in draft:rcpt_pick:toggle callback data: {data}")
                return True

            session = get_recipient_picker_session(
                chat_id=chat_id,
                user_id=user_id,
                draft_id=draft_id,
                field=field,
            )
            if not session:
                await _render_recipient_picker(
                    client=client,
                    chat_id=chat_id,
                    message_id=message_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            emails = list(session.get("emails") or [])
            if 0 <= idx < len(emails):
                selected = set(session.get("selected") or set())
                if idx in selected:
                    selected.remove(idx)
                else:
                    selected.add(idx)
                session["selected"] = selected

            await _render_recipient_picker(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                draft_id=draft_id,
                field=field,
            )
            return True

        if action == "page":
            if len(parts) < 6:
                logger.warning(f"Missing page index in draft:rcpt_pick:page callback data: {data}")
                return True
            try:
                page = int(parts[5])
            except Exception:
                logger.warning(f"Invalid page index in draft:rcpt_pick:page callback data: {data}")
                return True

            session = get_recipient_picker_session(
                chat_id=chat_id,
                user_id=user_id,
                draft_id=draft_id,
                field=field,
            )
            if not session:
                await _render_recipient_picker(
                    client=client,
                    chat_id=chat_id,
                    message_id=message_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            session["page"] = page
            await _render_recipient_picker(
                client=client,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                draft_id=draft_id,
                field=field,
            )
            return True

        if action == "cancel":
            clear_recipient_picker_session(
                chat_id=chat_id,
                user_id=user_id,
                draft_id=draft_id,
                field=field,
            )
            try:
                await client.edit_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=_("operation_cancelled"),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                )
            except Exception:
                pass
            return True

        if action == "save":
            session = get_recipient_picker_session(
                chat_id=chat_id,
                user_id=user_id,
                draft_id=draft_id,
                field=field,
            )
            if not session:
                await _render_recipient_picker(
                    client=client,
                    chat_id=chat_id,
                    message_id=message_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            target_field = get_recipient_target_field(field)
            if not target_field:
                clear_recipient_picker_session(
                    chat_id=chat_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            db = DBManager()
            conn = db._get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT account_id, chat_id, thread_id, card_message_id, status FROM drafts WHERE id = ?",
                (draft_id,),
            )
            draft_row = cur.fetchone()
            conn.close()
            if not draft_row:
                clear_recipient_picker_session(
                    chat_id=chat_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            account_id, draft_chat_id, draft_thread_id, card_message_id, status = draft_row
            if str(status) != "open":
                clear_recipient_picker_session(
                    chat_id=chat_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            refreshed = db.get_active_draft(
                chat_id=int(draft_chat_id),
                thread_id=int(draft_thread_id),
            )
            if not refreshed:
                clear_recipient_picker_session(
                    chat_id=chat_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            merged_addrs = merge_recipient_picker_selection(
                existing_addrs=refreshed.get(target_field),
                candidate_emails=list(session.get("emails") or []),
                selected_indices=set(session.get("selected") or set()),
            )
            conversation_matched, conversation_advanced, conversation_value = (
                await _submit_picker_selection_to_active_conversation(
                    chat_id=chat_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    draft_thread_id=int(draft_thread_id or 0),
                    field=field,
                    value=merged_addrs,
                )
            )
            if conversation_matched and not conversation_advanced:
                await _render_recipient_picker(
                    client=client,
                    chat_id=chat_id,
                    message_id=message_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    field=field,
                )
                return True

            if conversation_matched:
                merged_addrs = conversation_value

            db.update_draft(
                draft_id=draft_id,
                updates={target_field: merged_addrs},
            )

            refreshed = db.get_active_draft(
                chat_id=int(draft_chat_id), thread_id=int(draft_thread_id)
            )
            if refreshed and card_message_id:
                account = db.get_account(id=int(account_id))
                sig_label = format_signature_choice_label(
                    (account or {}).get("signature"),
                    get_draft_signature_choice(draft_id=int(refreshed["id"])),
                )
                attachments = db.list_draft_attachments(draft_id=refreshed["id"])
                card_text = _build_draft_card_text(
                    draft=refreshed,
                    attachments_count=len(attachments),
                    signature_label=sig_label,
                )
                try:
                    await client.edit_text(
                        chat_id=int(draft_chat_id),
                        message_id=int(card_message_id),
                        text=card_text,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        clear_draft=False,
                        reply_markup=ReplyMarkupInlineKeyboard(
                            rows=_build_draft_card_keyboard(draft_id=int(refreshed["id"]))
                        ),
                    )
                except Exception as e:
                    logger.error(f"Failed to update draft card after rcpt_pick save: {e}")

            clear_recipient_picker_session(
                chat_id=chat_id,
                user_id=user_id,
                draft_id=draft_id,
                field=field,
            )

            field_label = {
                "to": _("email_to"),
                "cc": _("email_cc"),
                "bcc": _("email_bcc"),
            }.get(field, field.upper())
            selected_summary = merged_addrs or _("draft_recipient_picker_none_selected")
            if len(selected_summary) > 1200:
                selected_summary = f"{selected_summary[:1197]}..."

            try:
                await client.edit_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"✅ {field_label}: {selected_summary}",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                )
            except Exception:
                pass
            return True

        logger.warning(f"Unknown draft:rcpt_pick action '{action}' in callback data: {data}")
        return True

    if data.startswith("draft:set_rcpt:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:set_rcpt' callback query: {e}")

        try:
            _p = data.split(":", 4)
            draft_id = int(_p[2])
            field = (_p[3] or "").strip().lower()
            token = (_p[4] or "").strip().lower()
        except Exception:
            logger.warning(f"Invalid draft:set_rcpt callback data: {data}")
            return True

        field_map = {
            "to": "to_addrs",
            "cc": "cc_addrs",
            "bcc": "bcc_addrs",
        }
        target_field = field_map.get(field)
        if not target_field or not token:
            return True

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT account_id, chat_id, thread_id, card_message_id, status FROM drafts WHERE id = ?",
            (draft_id,),
        )
        draft_row = cur.fetchone()
        conn.close()
        if not draft_row:
            logger.warning(f"Draft not found for set_rcpt: {draft_id}")
            return True

        account_id, draft_chat_id, draft_thread_id, card_message_id, status = draft_row
        if str(status) != "open":
            return True

        contacts = list_draft_contacts(
            db=db,
            account_id=int(account_id),
            query="",
            limit=2000,
        )
        selected_email = resolve_contact_email_by_token(
            field=field,
            contacts=contacts,
            token=token,
        )
        if not selected_email:
            logger.info(f"No matching contact for set_rcpt token: {token}")
            return True

        refreshed = db.get_active_draft(
            chat_id=int(draft_chat_id),
            thread_id=int(draft_thread_id),
        )
        if not refreshed:
            return True

        merged_addrs = append_contact_email(
            existing_addrs=refreshed.get(target_field),
            email_addr=selected_email,
        )
        db.update_draft(
            draft_id=draft_id,
            updates={target_field: merged_addrs},
        )

        refreshed = db.get_active_draft(
            chat_id=int(draft_chat_id), thread_id=int(draft_thread_id)
        )
        if refreshed and card_message_id:
            account = db.get_account(id=int(refreshed["account_id"]))
            sig_label = format_signature_choice_label(
                (account or {}).get("signature"),
                get_draft_signature_choice(draft_id=int(refreshed["id"])),
            )
            attachments = db.list_draft_attachments(draft_id=refreshed["id"])
            card_text = _build_draft_card_text(
                draft=refreshed,
                attachments_count=len(attachments),
                signature_label=sig_label,
            )
            try:
                await client.edit_text(
                    chat_id=int(draft_chat_id),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(
                        rows=_build_draft_card_keyboard(draft_id=int(refreshed["id"]))
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to update draft card after set_rcpt: {e}")

        try:
            field_label = {
                "to": _("email_to"),
                "cc": _("email_cc"),
                "bcc": _("email_bcc"),
            }.get(field, field.upper())
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ {field_label}: {selected_email}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception:
            pass
        return True

    if data.startswith("draft:att:rm:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:att:rm' callback query: {e}")

        try:
            _p = data.split(":", 4)
            draft_id = int(_p[3])
            attachment_id = int(_p[4])
        except Exception:
            logger.warning(f"Invalid draft:att:rm callback data: {data}")
            return True

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT chat_id, thread_id, card_message_id, status FROM drafts WHERE id = ?",
            (int(draft_id),),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            logger.warning(f"Draft not found for attachment removal: {draft_id}")
            return True

        draft_chat_id, draft_thread_id, card_message_id, status = row
        if str(status) != "open":
            return True

        db.delete_draft_attachment(draft_id=draft_id, attachment_id=attachment_id)

        refreshed = db.get_active_draft(
            chat_id=int(draft_chat_id), thread_id=int(draft_thread_id)
        )
        if refreshed and card_message_id:
            attachments = db.list_draft_attachments(draft_id=refreshed["id"])
            account = db.get_account(id=int(refreshed["account_id"]))
            sig_label = format_signature_choice_label(
                (account or {}).get("signature"),
                get_draft_signature_choice(draft_id=int(refreshed["id"])),
            )
            card_text = _build_draft_card_text(
                draft=refreshed,
                attachments_count=len(attachments),
                signature_label=sig_label,
            )
            try:
                await client.edit_text(
                    chat_id=int(draft_chat_id),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(
                        rows=_build_draft_card_keyboard(draft_id=int(refreshed["id"]))
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to update draft card after attachment removal: {e}")

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ {_('draft_attachment_removed')}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception:
            pass
        return True

    if data.startswith("draft:att:clear:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:att:clear' callback query: {e}")

        try:
            draft_id = int(data.split(":", 3)[3])
        except Exception:
            logger.warning(f"Invalid draft:att:clear callback data: {data}")
            return True

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT chat_id, thread_id, card_message_id, status FROM drafts WHERE id = ?",
            (int(draft_id),),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            logger.warning(f"Draft not found for attachment clear: {draft_id}")
            return True

        draft_chat_id, draft_thread_id, card_message_id, status = row
        if str(status) != "open":
            return True

        db.clear_draft_attachments(draft_id=draft_id)

        refreshed = db.get_active_draft(
            chat_id=int(draft_chat_id), thread_id=int(draft_thread_id)
        )
        if refreshed and card_message_id:
            account = db.get_account(id=int(refreshed["account_id"]))
            sig_label = format_signature_choice_label(
                (account or {}).get("signature"),
                get_draft_signature_choice(draft_id=int(refreshed["id"])),
            )
            card_text = _build_draft_card_text(
                draft=refreshed,
                attachments_count=0,
                signature_label=sig_label,
            )
            try:
                await client.edit_text(
                    chat_id=int(draft_chat_id),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(
                        rows=_build_draft_card_keyboard(draft_id=int(refreshed["id"]))
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to update draft card after attachment clear: {e}")

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ {_('draft_attachments_cleared')}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception:
            pass
        return True

    if data.startswith("draft:send:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'draft:send' callback query: {e}")

        try:
            draft_id = int(data.split(":", 2)[2])
        except Exception:
            logger.warning(f"Invalid draft id in callback data: {data}")
            return True

        def _parse_addrs(value: str | None) -> list[str]:
            if not value:
                return []
            parts: list[str] = []
            for chunk in value.replace("\n", ",").split(","):
                chunk = chunk.strip()
                if chunk:
                    parts.append(chunk)
            return parts

        db = DBManager()
        conn = db._get_connection()
        conn.row_factory = None
        cur = conn.cursor()
        cur.execute(
            "SELECT account_id, chat_id, thread_id, draft_type, from_identity_email, to_addrs, cc_addrs, bcc_addrs, subject, body_markdown, in_reply_to, references_header, status FROM drafts WHERE id = ?",
            (draft_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            logger.warning(f"Draft not found: {draft_id}")
            return True

        (
            account_id,
            draft_chat_id,
            draft_thread_id,
            draft_type,
            from_identity_email,
            to_addrs,
            cc_addrs,
            bcc_addrs,
            subject,
            body_markdown,
            in_reply_to,
            references_header,
            status,
        ) = row
        if status != "open":
            logger.info(f"Draft {draft_id} is not open; status={status}")
            return True

        account_manager = AccountManager()
        account = account_manager.get_account(id=account_id)
        if not account:
            logger.warning(f"Account not found for draft {draft_id}: {account_id}")
            return True

        from_name = account.get("alias") or account["email"]
        reply_to = None
        try:
            conn = db._get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT display_name, reply_to
                FROM account_identities
                WHERE account_id = ? AND from_email = ?
                """,
                (int(account_id), (from_identity_email or "").strip().lower()),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                display_name, db_reply_to = row
                if display_name:
                    from_name = str(display_name).strip() or from_name
                if db_reply_to:
                    reply_to = str(db_reply_to).strip() or None
        except Exception as e:
            logger.debug(f"Failed to resolve identity display name/reply-to: {e}")

        smtp = SMTPClient(
            server=account["smtp_server"],
            port=account["smtp_port"],
            username=account["email"],
            password=account["password"],
            use_ssl=bool(account["smtp_ssl"]),
        )

        signature_choice = normalize_signature_choice(
            account.get("signature"),
            get_draft_signature_choice(draft_id=draft_id),
        )
        set_draft_signature_choice(draft_id=draft_id, choice=signature_choice)
        signature_markdown, _signature_label = resolve_signature_for_send(
            account.get("signature"),
            signature_choice,
        )
        body_with_signature = _append_signature_to_markdown(
            body_markdown,
            signature_markdown,
        )

        html_body = None
        if body_with_signature and str(body_with_signature).strip():
            html_body = render_markdown_to_html(body_with_signature)

        refs = None
        if references_header and str(references_header).strip():
            refs = [p for p in str(references_header).split() if p]

        # Generate a stable Message-ID for threading, so future incoming replies can
        # be grouped into the same Telegram topic via In-Reply-To / References.
        try:
            domain = (from_identity_email or "").split("@", 1)[1].strip() or None
        except Exception:
            domain = None
        outgoing_message_id = make_msgid(domain=domain) if domain else make_msgid()
        outgoing_date = formatdate(localtime=True)

        attachments_payload = []
        try:
            rows = db.list_draft_attachments(draft_id=draft_id)
        except Exception:
            rows = []

        for att in rows[:40]:
            att_file_id = att.get("file_id")
            att_remote_id = att.get("remote_id")
            att_name = att.get("file_name") or "attachment"
            att_mime = att.get("mime_type") or "application/octet-stream"
            if not att_file_id and not att_remote_id:
                continue

            tg_file = None
            try:
                if att_file_id:
                    tg_file = await client.api.download_file(
                        int(att_file_id),
                        priority=1,
                        offset=0,
                        limit=0,
                        synchronous=True,
                    )
                elif att_remote_id:
                    tg_file = await client.api.get_remote_file(str(att_remote_id))
                    tg_file = await client.api.download_file(
                        int(tg_file.id),
                        priority=1,
                        offset=0,
                        limit=0,
                        synchronous=True,
                    )
            except Exception:
                if att_remote_id:
                    try:
                        tg_file = await client.api.get_remote_file(str(att_remote_id))
                        tg_file = await client.api.download_file(
                            int(tg_file.id),
                            priority=1,
                            offset=0,
                            limit=0,
                            synchronous=True,
                        )
                    except Exception as e:
                        logger.error(f"Failed to download attachment {att_name}: {e}")
                        return True
                else:
                    logger.error(f"Failed to download attachment {att_name}")
                    return True

            path = getattr(getattr(tg_file, "local", None), "path", None)
            if not path or not os.path.exists(path):
                logger.error(f"Downloaded attachment path missing for {att_name}: {path}")
                return True

            try:
                with open(path, "rb") as f:
                    data_bytes = f.read()
            except Exception as e:
                logger.error(f"Failed to read attachment file {att_name}: {e}")
                return True

            attachments_payload.append(
                {
                    "filename": str(att_name),
                    "mime_type": str(att_mime),
                    "data": data_bytes,
                }
            )

        ok = smtp.send_email_sync(
            from_email=from_identity_email,
            from_name=from_name,
            to_addrs=_parse_addrs(to_addrs),
            cc_addrs=_parse_addrs(cc_addrs),
            bcc_addrs=_parse_addrs(bcc_addrs),
            subject=subject or "",
            text_body=body_with_signature or "",
            html_body=html_body,
            reply_to=reply_to,
            in_reply_to=in_reply_to or None,
            references=refs,
            message_id=outgoing_message_id,
            date=outgoing_date,
            attachments=attachments_payload,
        )

        if ok:
            db.update_draft(draft_id=draft_id, updates={"status": "sent"})
            set_account_last_signature_choice(
                account_id=int(account_id),
                choice=resolve_signature_choice_to_store(
                    account.get("signature"),
                    signature_choice,
                ),
            )
            clear_draft_signature_choice(draft_id=draft_id)

            # Persist an outgoing email row so we can thread future incoming replies
            # back into this same topic (and show sent emails as their own history).
            try:
                sender_value = f"{from_name} <{from_identity_email}>"
                db.upsert_outgoing_email(
                    account_id=int(account_id),
                    message_id=str(outgoing_message_id),
                    telegram_thread_id=int(draft_thread_id),
                    sender=sender_value,
                    recipient=str(to_addrs or ""),
                    cc=str(cc_addrs or ""),
                    bcc=str(bcc_addrs or ""),
                    subject=str(subject or ""),
                    email_date=str(outgoing_date),
                    body_text=str(body_with_signature or ""),
                    body_html=str(html_body or ""),
                    in_reply_to=(str(in_reply_to).strip() if in_reply_to else None),
                    references_header=(
                        str(references_header).strip() if references_header else None
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to persist outgoing email row: {e}")

            # For compose drafts, turn the draft topic into a real email topic by renaming it.
            if str(draft_type) == "compose" and int(draft_thread_id or 0) > 0:
                try:
                    topic_title = (subject or "").strip() or _("no_subject")
                    topic_title = " ".join(topic_title.split())
                    if len(topic_title) > 128:
                        topic_title = topic_title[:125] + "..."
                    await client.api.edit_forum_topic(
                        chat_id=int(draft_chat_id),
                        message_thread_id=int(draft_thread_id),
                        # aiotdlib requires icon_custom_emoji_id even if we only rename.
                        icon_custom_emoji_id=0,
                        edit_icon_custom_emoji=False,
                        name=topic_title,
                    )
                except Exception as e:
                    logger.debug(f"Failed to rename compose topic after send: {e}")

            # Post a clean "sent email" representation into the topic,
            # then re-post attachments under it (like incoming emails).
            card_sent = False
            try:
                subject_display = (subject or "").strip() or _("no_subject")
                to_line = (to_addrs or "").strip()
                cc_line = (cc_addrs or "").strip()
                bcc_line = (bcc_addrs or "").strip()
                body = (body_with_signature or "").strip()

                card_html = build_outgoing_email_card(
                    subject=subject_display,
                    from_display=f"{from_name} <{from_identity_email}>",
                    to_addrs=to_line,
                    cc_addrs=cc_line,
                    bcc_addrs=bcc_line,
                    body_text=body,
                )
                sent = False
                parse_fn = getattr(client.api, "parse_text_entities", None)
                if callable(parse_fn):
                    try:
                        formatted = await parse_fn(
                            text=card_html,
                            parse_mode=(TextParseModeHTML()),
                        )
                        await client.api.send_message(
                            chat_id=int(draft_chat_id),
                            message_thread_id=int(draft_thread_id),
                            input_message_content=InputMessageText(text=formatted),
                        )
                        sent = True
                    except Exception as parse_err:
                        logger.debug(
                            f"Failed to send formatted sent-email card, falling back to plain text: {parse_err}"
                        )

                if not sent:
                    plain_lines: list[str] = [
                        f"OUT 📤 {subject_display}",
                        f"✍️ {_('email_from')}: {from_name} <{from_identity_email}>",
                    ]
                    if to_line:
                        plain_lines.append(f"📮 {_('email_to')}: {to_line}")
                    if cc_line:
                        plain_lines.append(f"👥 {_('email_cc')}: {cc_line}")
                    if bcc_line:
                        plain_lines.append(f"🔒 {_('email_bcc')}: {bcc_line}")
                    if body:
                        plain_lines.append("────────────")
                        plain_lines.append(body)

                    plain_text = "\n".join([l for l in plain_lines if l]).strip()
                    max_len = 4000
                    if len(plain_text) > max_len:
                        suffix = f"...\n\n{_('content_truncated')}"
                        head = plain_text[: max(0, max_len - len(suffix))].rstrip()
                        plain_text = head + suffix

                    await client.api.send_message(
                        chat_id=int(draft_chat_id),
                        message_thread_id=int(draft_thread_id),
                        input_message_content=InputMessageText(
                            text=FormattedText(text=plain_text, entities=[])
                        ),
                    )
                card_sent = True
            except Exception as e:
                logger.error(f"Failed to send sent-email card to topic: {e}")

            attachments_sent = False
            if card_sent:
                attachments_sent = True
                for att in (rows or [])[:40]:
                    att_file_id = att.get("file_id")
                    att_remote_id = att.get("remote_id")
                    if not att_file_id and not att_remote_id:
                        continue
                    try:
                        input_file = (
                            InputFileId(id=int(att_file_id))
                            if att_file_id
                            else InputFileRemote(id=str(att_remote_id))
                        )
                        await client.api.send_message(
                            chat_id=int(draft_chat_id),
                            message_thread_id=int(draft_thread_id),
                            input_message_content=InputMessageDocument(
                                document=input_file,
                                caption=FormattedText(text="", entities=[]),
                            ),
                        )
                    except Exception as e:
                        attachments_sent = False
                        logger.error(f"Failed to send outgoing attachment to topic: {e}")
                        break

            # Clean up draft UI messages (draft card + user typed body/commands). We also
            # delete the original attachment messages if we successfully re-posted them
            # under the sent-email card.
            tracked_text_ids: list[int] = []
            tracked_attachment_ids: list[int] = []
            try:
                conn = db._get_connection()
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT message_id, message_type
                    FROM draft_messages
                    WHERE draft_id = ?
                    ORDER BY message_id ASC
                    """,
                    (int(draft_id),),
                )
                tracked_rows = cur.fetchall()
                conn.close()
            except Exception:
                tracked_rows = []
            for row in tracked_rows:
                try:
                    mid = int(row[0])
                except Exception:
                    continue
                mtype = (str(row[1] or "").strip() or "").lower()
                if not mtype or mtype == "text":
                    tracked_text_ids.append(mid)
                else:
                    tracked_attachment_ids.append(mid)

            to_delete = {int(message_id)}
            for mid in tracked_text_ids:
                to_delete.add(int(mid))
            if attachments_sent:
                for mid in tracked_attachment_ids:
                    to_delete.add(int(mid))

            delete_ids = sorted([mid for mid in to_delete if int(mid) > 0])
            if delete_ids:
                try:
                    await client.api.delete_messages(
                        chat_id=int(draft_chat_id),
                        message_ids=delete_ids,
                        revoke=True,
                    )
                except Exception as e:
                    logger.error(f"Failed to delete draft messages after send: {e}")
            try:
                db.clear_draft_messages(draft_id=draft_id)
            except Exception:
                pass
        else:
            try:
                await client.api.answer_callback_query(
                    update.id, text=_("send_failed"), url="", cache_time=1
                )
            except Exception as e:
                logger.warning(f"Failed to answer send failure callback: {e}")

        return True

    return False
