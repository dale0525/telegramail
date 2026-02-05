import asyncio
import os
from email.utils import formatdate, make_msgid

from aiotdlib import Client
from aiotdlib.api import (
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    LinkPreviewOptions,
    InputMessageText,
    FormattedText,
    ReplyMarkupInlineKeyboard,
    UpdateNewCallbackQuery,
)

from app.bot.utils import answer_callback
from app.database import DBManager
from app.email_utils.account_manager import AccountManager
from app.email_utils.markdown_render import render_markdown_to_html
from app.email_utils.smtp_client import SMTPClient
from app.i18n import _
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
            body = refreshed.get("body_markdown") or ""
            card_text = (
                f"📝 {_('draft')}\n\n"
                f"From: {refreshed.get('from_identity_email') or ''}\n"
                f"To: {refreshed.get('to_addrs') or ''}\n"
                f"Cc: {refreshed.get('cc_addrs') or ''}\n"
                f"Bcc: {refreshed.get('bcc_addrs') or ''}\n"
                f"Subject: {refreshed.get('subject') or ''}\n"
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
                    chat_id=int(draft_chat_id),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
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
            body = refreshed.get("body_markdown") or ""
            card_text = (
                f"📝 {_('draft')}\n\n"
                f"From: {refreshed.get('from_identity_email') or ''}\n"
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
                    chat_id=int(draft_chat_id),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
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
            body = refreshed.get("body_markdown") or ""
            card_text = (
                f"📝 {_('draft')}\n\n"
                f"From: {refreshed.get('from_identity_email') or ''}\n"
                f"To: {refreshed.get('to_addrs') or ''}\n"
                f"Cc: {refreshed.get('cc_addrs') or ''}\n"
                f"Bcc: {refreshed.get('bcc_addrs') or ''}\n"
                f"Subject: {refreshed.get('subject') or ''}\n"
                f"{_('draft_attachments')}: 0\n"
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
                    chat_id=int(draft_chat_id),
                    message_id=int(card_message_id),
                    text=card_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    clear_draft=False,
                    reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
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

        html_body = None
        if body_markdown and str(body_markdown).strip():
            html_body = render_markdown_to_html(body_markdown)

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
            text_body=body_markdown or "",
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
                    body_text=str(body_markdown or ""),
                    body_html=str(html_body or ""),
                    in_reply_to=(str(in_reply_to).strip() if in_reply_to else None),
                    references_header=(
                        str(references_header).strip() if references_header else None
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to persist outgoing email row: {e}")

            # Clean up draft UI messages (draft card + user typed body/commands) so the topic
            # only keeps the final sent email presentation.
            try:
                tracked_ids = db.list_draft_message_ids(draft_id=draft_id)
            except Exception:
                tracked_ids = []
            to_delete = {int(message_id)}
            for mid in tracked_ids:
                try:
                    to_delete.add(int(mid))
                except Exception:
                    continue
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

            # Post a clean "sent email" representation into the topic.
            try:
                subject_display = (subject or "").strip() or _("no_subject")
                await client.api.send_message(
                    chat_id=int(draft_chat_id),
                    message_thread_id=int(draft_thread_id),
                    input_message_content=InputMessageText(
                        text=FormattedText(text=f"📤 {subject_display}", entities=[])
                    ),
                )
                await client.api.send_message(
                    chat_id=int(draft_chat_id),
                    message_thread_id=int(draft_thread_id),
                    input_message_content=InputMessageText(
                        text=FormattedText(
                            text=f"✍️ {_('email_from')}: {from_name} <{from_identity_email}>",
                            entities=[],
                        )
                    ),
                )

                to_line = (to_addrs or "").strip()
                if to_line:
                    await client.api.send_message(
                        chat_id=int(draft_chat_id),
                        message_thread_id=int(draft_thread_id),
                        input_message_content=InputMessageText(
                            text=FormattedText(
                                text=f"📮 {_('email_to')}: {to_line}", entities=[]
                            )
                        ),
                    )

                cc_line = (cc_addrs or "").strip()
                if cc_line:
                    await client.api.send_message(
                        chat_id=int(draft_chat_id),
                        message_thread_id=int(draft_thread_id),
                        input_message_content=InputMessageText(
                            text=FormattedText(
                                text=f"👥 {_('email_cc')}: {cc_line}", entities=[]
                            )
                        ),
                    )

                bcc_line = (bcc_addrs or "").strip()
                if bcc_line:
                    await client.api.send_message(
                        chat_id=int(draft_chat_id),
                        message_thread_id=int(draft_thread_id),
                        input_message_content=InputMessageText(
                            text=FormattedText(
                                text=f"🔒 {_('email_bcc')}: {bcc_line}", entities=[]
                            )
                        ),
                    )

                body = (body_markdown or "").strip()
                if body:
                    max_length = 4000
                    if len(body) > max_length:
                        body = body[:max_length] + f"...\n\n{_('content_truncated')}"
                    await client.api.send_message(
                        chat_id=int(draft_chat_id),
                        message_thread_id=int(draft_thread_id),
                        input_message_content=InputMessageText(
                            text=FormattedText(text=body, entities=[])
                        ),
                    )
            except Exception as e:
                logger.error(f"Failed to send sent-email card to topic: {e}")
        else:
            try:
                await client.api.answer_callback_query(
                    update.id, text=_("send_failed"), url="", cache_time=1
                )
            except Exception as e:
                logger.warning(f"Failed to answer send failure callback: {e}")

        return True

    return False
