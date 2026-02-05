import json
from email.utils import getaddresses

from aiotdlib import Client
from aiotdlib.api import (
    FormattedText,
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    InputMessageText,
    ReplyMarkupInlineKeyboard,
    UpdateNewCallbackQuery,
)

from app.bot.utils import answer_callback
from app.database import DBManager
from app.email_utils.account_manager import AccountManager
from app.email_utils.identity import choose_recommended_from
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def handle_email_action_callback(
    *, client: Client, update: UpdateNewCallbackQuery, data: str
) -> bool:
    chat_id = update.chat_id
    message_id = update.message_id

    account_manager = AccountManager()

    if data.startswith("email:reply:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'email:reply' callback query: {e}")

        try:
            _p = data.split(":", 3)
            email_id = int(_p[2])
            thread_id = int(_p[3])
        except Exception:
            logger.warning(f"Invalid email reply callback data: {data}")
            return True

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT email_account, sender, subject, message_id, delivered_to
            FROM emails
            WHERE id = ?
            """,
            (email_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            logger.warning(f"Email not found for reply: {email_id}")
            return True

        account_id, sender, subject, orig_message_id, delivered_to = row
        account = account_manager.get_account(id=account_id)
        if not account:
            logger.warning(f"Account not found for reply draft: {account_id}")
            return True

        identities = db.list_account_identities(account_id=account_id)
        identity_emails = {i["from_email"] for i in identities}
        try:
            candidates = json.loads(delivered_to) if delivered_to else []
        except Exception:
            candidates = []

        from_email = choose_recommended_from(
            candidates=candidates,
            identity_emails=identity_emails,
            default_email=account["email"],
        )

        to_email = ""
        try:
            addrs = getaddresses([sender or ""])
            for _name, addr in addrs:
                addr = (addr or "").strip().lower()
                if addr and "@" in addr:
                    to_email = addr
                    break
        except Exception:
            to_email = ""

        subject = subject or ""
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}".strip()

        draft_id = db.create_draft(
            account_id=account_id,
            chat_id=chat_id,
            thread_id=thread_id,
            draft_type="reply",
            from_identity_email=from_email,
        )
        db.update_draft(
            draft_id=draft_id,
            updates={
                "to_addrs": to_email,
                "subject": subject,
                "in_reply_to": orig_message_id or None,
                "references_header": orig_message_id or None,
            },
        )

        draft = db.get_active_draft(chat_id=chat_id, thread_id=thread_id)
        card_text = (
            f"📝 {_('draft')}\n\n"
            f"From: {from_email}\n"
            f"To: {draft.get('to_addrs') or ''}\n"
            f"Subject: {draft.get('subject') or ''}\n\n"
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
        try:
            msg = await client.api.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                input_message_content=InputMessageText(
                    text=FormattedText(text=card_text, entities=[])
                ),
                reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
            )
            db.update_draft(draft_id=draft_id, updates={"card_message_id": msg.id})
        except Exception as e:
            logger.error(f"Failed to send reply draft card: {e}")

        return True

    if data.startswith("email:forward:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'email:forward' callback query: {e}")

        try:
            _p = data.split(":", 3)
            email_id = int(_p[2])
            thread_id = int(_p[3])
        except Exception:
            logger.warning(f"Invalid email forward callback data: {data}")
            return True

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT email_account, sender, recipient, cc, subject, email_date, body_text, delivered_to
            FROM emails
            WHERE id = ?
            """,
            (email_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            logger.warning(f"Email not found for forward: {email_id}")
            return True

        (
            account_id,
            sender,
            recipient,
            cc,
            subject,
            email_date,
            body_text,
            delivered_to,
        ) = row
        account = account_manager.get_account(id=account_id)
        if not account:
            logger.warning(f"Account not found for forward draft: {account_id}")
            return True

        identities = db.list_account_identities(account_id=account_id)
        identity_emails = {i["from_email"] for i in identities}
        try:
            candidates = json.loads(delivered_to) if delivered_to else []
        except Exception:
            candidates = []

        from_email = choose_recommended_from(
            candidates=candidates,
            identity_emails=identity_emails,
            default_email=account["email"],
        )

        original_subject = subject or ""
        draft_subject = original_subject
        if not draft_subject.lower().startswith("fwd:") and not draft_subject.lower().startswith("fw:"):
            draft_subject = f"Fwd: {draft_subject}".strip()

        quoted = (body_text or "").strip()
        if quoted:
            quoted = "\n".join(["> " + line for line in quoted.splitlines()])

        header_lines: list[str] = [
            "---------- Forwarded message ----------",
            f"From: {sender}",
            f"Date: {email_date or ''}",
            f"Subject: {original_subject}",
            f"To: {recipient or ''}",
        ]
        if cc and str(cc).strip():
            header_lines.append(f"Cc: {cc}")
        forward_body = (
            "\n".join(header_lines) + ("\n\n" + quoted if quoted else "")
        ).strip()

        draft_id = db.create_draft(
            account_id=account_id,
            chat_id=chat_id,
            thread_id=thread_id,
            draft_type="forward",
            from_identity_email=from_email,
        )
        db.update_draft(
            draft_id=draft_id,
            updates={
                "to_addrs": "",
                "subject": draft_subject,
                "body_markdown": forward_body,
            },
        )

        draft = db.get_active_draft(chat_id=chat_id, thread_id=thread_id)
        card_text = (
            f"📝 {_('draft')}\n\n"
            f"From: {from_email}\n"
            f"To: \n"
            f"Subject: {draft.get('subject') or ''}\n\n"
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
        try:
            msg = await client.api.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                input_message_content=InputMessageText(
                    text=FormattedText(text=card_text, entities=[])
                ),
                reply_markup=ReplyMarkupInlineKeyboard(rows=keyboard),
            )
            db.update_draft(draft_id=draft_id, updates={"card_message_id": msg.id})
        except Exception as e:
            logger.error(f"Failed to send forward draft card: {e}")

        return True

    return False

