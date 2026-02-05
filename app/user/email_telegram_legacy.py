import os
import re
from typing import Any, Dict

from app.email_utils import (
    AccountManager,
    format_enhanced_email_summary,
    summarize_email,
)
from app.email_utils import extract_unsubscribe_urls
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def send_email_to_telegram_legacy(sender: Any, email_data: Dict[str, Any]) -> bool:
    """
    Legacy email sending method (kept as backup).

    This is extracted from `EmailTelegramSender.send_email_to_telegram_legacy` to keep the
    main module under 1000 lines.
    """
    try:
        account_id = email_data["email_account"]
        account_manager = AccountManager()
        account = account_manager.get_account(id=account_id)
        group_id = account["tg_group_id"]

        # Clean subject (remove Re: prefix)
        subject = email_data["subject"]
        clean_subject = re.sub(r"^(?i)(re|fw|fwd|回复|转发)[:：]\s*", "", subject.strip())

        # Decode sender name
        original_sender = email_data.get("sender", "")
        decoded_sender = sender.decode_mime_header_value(original_sender)

        # Check for existing thread ID
        thread_id = await sender.get_thread_id_by_subject(clean_subject, account_id)

        # If no thread exists, create a new forum topic
        if not thread_id:
            thread_id = await sender.create_forum_topic(group_id, clean_subject)
            if not thread_id:
                logger.error("Failed to create forum topic")
                return False

        # Update database with thread ID
        sender.db_manager.update_thread_id_in_db(email_data["id"], thread_id)

        # 2. Send email title
        await sender.send_text_message(
            chat_id=group_id,
            text=f"*{email_data['subject']}*",
            thread_id=thread_id,
            parse_mode="Markdown",
            send_notification=False,
        )

        # 3. Send email headers - From
        await sender.send_text_message(
            chat_id=group_id,
            text=f"✍️ {_('email_from')}: {decoded_sender}",
            thread_id=thread_id,
            send_notification=False,
        )

        # 4. CC (if exists)
        if email_data.get("cc"):
            await sender.send_text_message(
                chat_id=group_id,
                text=f"👥 {_('email_cc')}: {email_data['cc']}",
                thread_id=thread_id,
                send_notification=False,
            )

        # 5. BCC (if exists)
        if email_data.get("bcc"):
            await sender.send_text_message(
                chat_id=group_id,
                text=f"🔒 {_('email_bcc')}: {email_data['bcc']}",
                thread_id=thread_id,
                send_notification=False,
            )

        # 6. Email Summary - Use enhanced processing logic, prioritize HTML, fallback to plain text
        processed_content = sender.get_processed_email_content(email_data)
        if processed_content:
            unsubscribe_urls = []
            body_html = email_data.get("body_html", "")
            if body_html and body_html.strip():
                unsubscribe_urls = extract_unsubscribe_urls(
                    body_html, default_language=os.getenv("DEFAULT_LANGUAGE", "en_US")
                )

            summary = summarize_email(processed_content, extra_urls=unsubscribe_urls)
            if summary is not None:
                formatted_summary = format_enhanced_email_summary(summary)
                summary_header = f"<b>{_('email_summary')}:</b>\n"

                await sender.send_text_message(
                    chat_id=group_id,
                    text=f"{summary_header}{formatted_summary}",
                    urls=summary.get("urls", []),
                    thread_id=thread_id,
                    parse_mode="HTML",
                    send_notification=True,
                )
            else:
                max_length = 4000  # Telegram message length limit
                if len(processed_content) > max_length:
                    truncated_content = (
                        processed_content[:max_length] + f"...\n\n{_('content_truncated')}"
                    )
                else:
                    truncated_content = processed_content

                await sender.send_text_message(
                    chat_id=group_id,
                    text=truncated_content,
                    urls=unsubscribe_urls,
                    thread_id=thread_id,
                    send_notification=True,
                )
        else:
            await sender.send_text_message(
                chat_id=group_id,
                text=f"📧 {_('email_content_unavailable')}",
                thread_id=thread_id,
                send_notification=True,
            )

        # 7. Send original HTML as a file attachment if needed
        if email_data.get("body_html"):
            sanitized_subject = re.sub(r'[\\/*?:"<>|]', "_", clean_subject)[:50]
            html_filename = f"{sanitized_subject}.html"
            await sender.send_html_as_file(
                chat_id=group_id,
                thread_id=thread_id,
                content=email_data["body_html"],
                filename=html_filename,
                send_notification=False,
            )

        # 8. Send attachments if any
        if email_data.get("attachments"):
            await sender.send_attachments(group_id, thread_id, email_data["attachments"])

        return True

    except Exception as e:
        logger.error(f"Error sending email to Telegram: {e}")
        return False
