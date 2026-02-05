import os
import re
from typing import Dict, List, Optional, Any
import tempfile
import email.header
import base64
import json

from app.bot.bot_client import BotClient
from app.i18n import _
from app.utils import Logger
from app.database import DBManager
from app.user.atomic_email_sender import (
    AtomicEmailSender,
    AttachmentContent,
    FileContent,
    MessageContent,
)
from app.user.email_telegram_legacy import (
    send_email_to_telegram_legacy as _send_email_to_telegram_legacy,
)
from app.user.user_client import UserClient
from app.email_utils import (
    summarize_email,
    format_enhanced_email_summary,
    AccountManager,
    clean_html_content,
    extract_unsubscribe_urls,
)
from app.email_utils.identity import suggest_identity
from aiotdlib.api import (
    FormattedText,
    InputMessageText,
    MessageSendOptions,
    InputMessageDocument,
    InputFileLocal,
    Message,
    ForumTopicIcon,
    TextParseModeHTML,
    TextParseModeMarkdown,
    InlineKeyboardButton,
    InlineKeyboardButtonTypeCallback,
    InlineKeyboardButtonTypeUrl,
    ReplyMarkupInlineKeyboard,
)

logger = Logger().get_logger(__name__)


class EmailTelegramSender:
    """Class for sending emails to Telegram chats"""

    def __init__(self):
        self.user_client = UserClient().client
        self.bot_client = BotClient().client
        self.db_manager = DBManager()

    async def get_thread_id_by_subject(
        self, clean_subject: str, account_id: int
    ) -> Optional[int]:
        """
        Find a telegram thread ID by email subject (without Re: prefix)

        Args:
            subject: Email subject to search for
            group_id: Telegram group ID to check

        Returns:
            Optional[int]: Thread ID if found, None otherwise
        """

        # Query database for an existing thread with this subject
        conn = None
        try:
            conn = self.db_manager._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT telegram_thread_id FROM emails
                WHERE subject LIKE ? AND telegram_thread_id IS NOT NULL AND email_account = ?
                LIMIT 1
                """,
                (f"%{clean_subject}%", account_id),
            )
            result = cursor.fetchone()
            if result and result[0]:
                # Convert string to int if it's a numeric string
                try:
                    return int(result[0])
                except (ValueError, TypeError):
                    logger.error(f"Failed to convert thread_id to integer: {result[0]}")
                    return None
            return None
        except Exception as e:
            logger.error(f"Error getting thread ID by subject: {e}")
            raise  # Let the decorator handle retries
        finally:
            # Always close the connection
            if conn:
                try:
                    conn.close()
                except Exception as close_error:
                    logger.error(f"Error closing database connection: {close_error}")

    async def create_forum_topic(self, chat_id: int, title: str) -> Optional[int]:
        """
        Create a forum topic in the specified chat

        Args:
            chat_id: Telegram chat ID (supergroup)
            title: Title for the forum topic (max 128 chars)

        Returns:
            Optional[int]: message_thread_id if created successfully, None otherwise
        """
        try:
            # Truncate title to 128 chars if needed
            if len(title) > 128:
                title = title[:125] + "..."

            # Create forum topic
            # 5377498341074542641:‼️
            # 5379748062124056162:❗️
            # 5309984423003823246:📣
            # 5237699328843200968:✅
            # 5235579393115438657:⭐️
            # 5417915203100613993:💬
            result = await self.bot_client.api.create_forum_topic(
                chat_id=chat_id,
                name=title,
                icon=ForumTopicIcon(
                    color=0x6FB9F0, custom_emoji_id=5309984423003823246
                ),
            )

            logger.info(
                f"Created forum topic '{title}' with message_thread_id: {result.message_thread_id}"
            )
            return result.message_thread_id
        except Exception as e:
            logger.error(f"Error creating forum topic: {e}")
            return None

    async def str_to_formatted(self, original: str, parse_mode: Any) -> FormattedText:
        if not isinstance(parse_mode, str):
            result = FormattedText(text=original, entities=[])
        elif parse_mode.lower() == "html":
            original = original.replace("<br>", "\n")
            result = await self.bot_client.api.parse_text_entities(
                text=original,
                parse_mode=(TextParseModeHTML()),
            )
        elif parse_mode.lower() == "markdown":
            # in case this is a title
            to_escape = original
            is_title = False
            if original.startswith("*") and original.endswith("*"):
                to_escape = original[1:-1]
                is_title = True
            to_escape = re.sub(
                r"[_*[\]()~>#\+\-=|{}.!]", lambda x: "\\" + x.group(), to_escape
            )
            if is_title:
                original = f"*{to_escape}*"
            else:
                original = to_escape
            result = await self.bot_client.api.parse_text_entities(
                text=original,
                parse_mode=(TextParseModeMarkdown(version=2)),
            )
        else:
            result = FormattedText(text=original, entities=[])
        return result

    async def send_text_message(
        self,
        chat_id: int,
        text: str,
        send_notification: bool = True,
        thread_id: Optional[int] = None,
        urls: Optional[list[dict]] = None,
        parse_mode: Optional[str] = None,
    ) -> Optional[Message]:
        """
        Send a text message to a Telegram chat

        Args:
            chat_id: Telegram chat ID
            text: Message text to send
            thread_id: Optional thread ID for forum topics
            send_notification: Whether to send notification
            urls: Optional list of URLs to include as buttons
            parse_mode: Optional parse mode (HTML, Markdown)

        Returns:
            Optional[Message]: Message object if sent successfully, None otherwise
        """
        buttons = []
        if urls and len(urls) > 0:
            for url in urls[:5]:
                url_text = url["caption"]
                url_link = url["link"]
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=url_text,
                            type=InlineKeyboardButtonTypeUrl(url=url_link),
                        )
                    ]
                )
        try:
            formatted = await self.str_to_formatted(text, parse_mode)
            send_kwargs = {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "input_message_content": InputMessageText(text=formatted),
                "options": MessageSendOptions(
                    paid_message_star_count=0,
                    sending_id=0,
                    disable_notification=not send_notification,
                    from_background=not send_notification,
                ),
            }
            # Only add reply_markup if buttons exist
            if len(buttons) > 0:
                send_kwargs["reply_markup"] = ReplyMarkupInlineKeyboard(rows=buttons)

            return await self.bot_client.api.send_message(**send_kwargs)
        except Exception as e:
            logger.error(f"Error sending text message: {e}")
            return None

    async def send_formatted_text_message(
        self,
        *,
        chat_id: int,
        formatted_text: FormattedText,
        send_notification: bool = True,
        thread_id: Optional[int] = None,
        urls: Optional[list[dict]] = None,
    ) -> Optional[Message]:
        """
        Send a pre-parsed FormattedText message to a Telegram chat

        This avoids parse-time failures (HTML/Markdown parsing) during the send phase,
        which helps prevent "empty topic" cases for new forum topics.
        """
        buttons = []
        if urls and len(urls) > 0:
            for url in urls[:5]:
                url_text = url["caption"]
                url_link = url["link"]
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=url_text,
                            type=InlineKeyboardButtonTypeUrl(url=url_link),
                        )
                    ]
                )
        try:
            send_kwargs = {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "input_message_content": InputMessageText(text=formatted_text),
                "options": MessageSendOptions(
                    paid_message_star_count=0,
                    sending_id=0,
                    disable_notification=not send_notification,
                    from_background=not send_notification,
                ),
            }
            if len(buttons) > 0:
                send_kwargs["reply_markup"] = ReplyMarkupInlineKeyboard(rows=buttons)

            return await self.bot_client.api.send_message(**send_kwargs)
        except Exception as e:
            logger.error(f"Error sending formatted text message: {e}")
            return None

    async def send_html_as_file(
        self,
        chat_id: int,
        thread_id: int,
        content: str,
        filename: str = "email.html",
        send_notification: bool = False,
    ) -> Optional[Message]:
        """
        Send HTML content as a file

        Args:
            chat_id: Telegram chat ID
            thread_id: Thread ID for forum topics
            content: HTML content
            filename: Name for the file
            send_notification: Whether to send telegram notification

        Returns:
            Optional[Message]: Message object if sent successfully, None otherwise
        """
        try:
            # Create a temporary directory
            temp_dir = tempfile.mkdtemp()
            temp_path = os.path.join(temp_dir, filename)

            # Write the content to the file with the specified filename
            # Ensure HTML content declares UTF-8
            html_content_lower = content.lower()
            # Check for common charset declarations in meta tags
            charset_declared = (
                '<meta charset="utf-8">' in html_content_lower
                or "charset=utf-8" in html_content_lower
                or '<meta charset="utf8">' in html_content_lower
                or "charset=utf8" in html_content_lower
            )

            if not charset_declared:
                # Attempt to inject the meta tag into the <head> section
                head_match = re.search(r"<head.*?>", content, re.IGNORECASE)
                if head_match:
                    inject_pos = head_match.end()
                    content = (
                        content[:inject_pos]
                        + '<meta charset="UTF-8">'
                        + content[inject_pos:]
                    )
                else:
                    # If no <head> tag, prepend to the whole content
                    content = '<meta charset="UTF-8">' + content

            with open(temp_path, "wb") as f:
                f.write(content.encode("utf-8"))

            # Send file
            message = await self.bot_client.api.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                input_message_content=InputMessageDocument(
                    document=InputFileLocal(path=temp_path),
                    caption=FormattedText(text=_("html_preview"), entities=[]),
                ),
                options=MessageSendOptions(
                    paid_message_star_count=0,
                    sending_id=0,
                    disable_notification=not send_notification,
                    from_background=not send_notification,
                ),
            )

            # Clean up
            os.unlink(temp_path)
            os.rmdir(temp_dir)

            return message
        except Exception as e:
            logger.error(f"Error sending HTML as file: {e}")
            # Clean up in case of error
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            if "temp_dir" in locals() and os.path.exists(temp_dir):
                os.rmdir(temp_dir)
            return None

    async def send_attachment(
        self, chat_id: int, thread_id: int, attachment: Dict[str, Any]
    ) -> Optional[Message]:
        """
        Send a single attachment to Telegram

        Args:
            chat_id: Telegram chat ID
            thread_id: Thread ID for forum topics
            attachment: Attachment data dictionary

        Returns:
            Optional[Message]: Message object if sent successfully, None otherwise
        """
        try:
            # Get original filename and decode if necessary
            encoded_filename = attachment["filename"]
            filename = self.decode_mime_filename(encoded_filename)

            # Create temporary directory and file with the decoded filename
            temp_dir = tempfile.mkdtemp()
            # Sanitize filename to avoid filesystem issues
            safe_filename = self.sanitize_filename(filename)
            temp_path = os.path.join(temp_dir, safe_filename)

            # Write attachment data to the file
            with open(temp_path, "wb") as f:
                f.write(attachment["data"])

            content = InputMessageDocument(
                document=InputFileLocal(path=temp_path),
                thumbnail=None,
                caption=FormattedText(text="", entities=[]),
            )

            # Send the message
            message = await self.bot_client.api.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                input_message_content=content,
                options=MessageSendOptions(
                    paid_message_star_count=0,
                    sending_id=0,
                    disable_notification=True,
                    from_background=True,
                ),
            )

            # Clean up
            os.unlink(temp_path)
            os.rmdir(temp_dir)

            return message
        except Exception as e:
            logger.error(f"Error sending attachment: {e}")
            # Clean up in case of error
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            if "temp_dir" in locals() and os.path.exists(temp_dir):
                os.rmdir(temp_dir)
            return None

    def decode_mime_header_value(self, value: str) -> str:
        """
        Decode a MIME encoded header value (like =?utf-8?B?...?=).

        Args:
            value: The potentially encoded header value.

        Returns:
            str: Decoded header value.
        """
        if not value:
            return ""
        try:
            # Check if this is a MIME encoded string (more robustly)
            if "=?" in value and "?=" in value:
                decoded_parts = email.header.decode_header(value)
                decoded_value_parts = []
                for part, charset in decoded_parts:
                    if isinstance(part, bytes):
                        if charset:
                            decoded_value_parts.append(
                                part.decode(charset, errors="replace")
                            )
                        else:
                            # Default to utf-8 if charset is not specified
                            decoded_value_parts.append(
                                part.decode("utf-8", errors="replace")
                            )
                    else:
                        decoded_value_parts.append(part)
                return "".join(decoded_value_parts).strip()

            # Return original if no encoding detected or needed
            return value.strip()
        except Exception as e:
            logger.error(f"Error decoding header value: {e}, using original: {value}")
            return value.strip()

    def decode_mime_filename(self, filename: str) -> str:
        """
        Decode MIME encoded filenames (like =?utf-8?B?...?=)

        Args:
            filename: The potentially encoded filename

        Returns:
            str: Decoded filename
        """
        try:
            # Check if this is a MIME encoded string
            if filename.startswith("=?") and filename.endswith("?="):
                # Use email.header.decode_header to decode
                decoded_parts = email.header.decode_header(filename)
                # Join all decoded parts
                decoded_filename = ""
                for part, charset in decoded_parts:
                    if isinstance(part, bytes):
                        if charset:
                            decoded_filename += part.decode(charset)
                        else:
                            # Default to utf-8 if charset is not specified
                            decoded_filename += part.decode("utf-8", errors="replace")
                    else:
                        decoded_filename += part
                return decoded_filename
            # Custom decoding for specific pattern not handled by email.header
            # Format: =?charset?encoding?encoded-text?=
            pattern = r"=\?([^?]+)\?([BbQq])\?([^?]+)\?\="
            match = re.match(pattern, filename)
            if match:
                charset, encoding, encoded_text = match.groups()
                if encoding.upper() == "B":  # Base64
                    decoded_text = base64.b64decode(encoded_text).decode(charset)
                    return decoded_text
                # Handle other encodings if needed (Q for quoted-printable)

            # Return original if no encoding detected
            return filename
        except Exception as e:
            logger.error(f"Error decoding filename: {e}, using original: {filename}")
            return filename

    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename to make it safe for file system operations

        Args:
            filename: Original filename that might contain invalid characters

        Returns:
            str: Sanitized filename
        """
        # Replace invalid characters with underscore
        sanitized = re.sub(r'[\\/*?:"<>|]', "_", filename)
        # Limit length to prevent extremely long filenames
        if len(sanitized) > 200:
            name, ext = os.path.splitext(sanitized)
            sanitized = name[:195] + ext
        # Ensure we have at least some filename
        if not sanitized or sanitized.isspace():
            sanitized = "attachment"
        return sanitized

    def get_processed_email_content(self, email_data: Dict[str, Any]) -> str:
        """
        Get processed email content with fallback mechanism from HTML to text

        Args:
            email_data: Email data dictionary

        Returns:
            str: Processed email content, prioritizing HTML over plain text
        """
        # Prioritize body_html (if exists and non-empty)
        body_html = email_data.get("body_html", "")
        if body_html and body_html.strip():
            logger.info("Using HTML content for email processing")
            processed_content = clean_html_content(body_html)
            if processed_content and processed_content.strip():
                return processed_content
            else:
                logger.warning(
                    "HTML preprocessing returned empty content, falling back to text"
                )

        # If body_html doesn't exist, is empty, or processing failed, use body_text as fallback
        body_text = email_data.get("body_text", "")
        if body_text and body_text.strip():
            logger.info("Using text content for email processing")
            return body_text.strip()

        # If both are unavailable, return empty string
        logger.warning("No usable email content found (both HTML and text are empty)")
        return ""

    def prepare_email_messages(
        self, email_data: Dict[str, Any]
    ) -> List[MessageContent]:
        """
        Prepare email messages for sending to Telegram

        Args:
            email_data: Email data dictionary

        Returns:
            List[MessageContent]: List of prepared message content
        """
        messages = []

        # 1. Email title
        subject = (email_data.get("subject") or "").strip() or _("no_subject")
        title_message = MessageContent(
            text=f"*📥 {subject}*",
            parse_mode="Markdown",
            send_notification=False,
        )
        messages.append(title_message)

        # 2. Sender information
        original_sender = email_data.get("sender", "")
        decoded_sender = self.decode_mime_header_value(original_sender)
        from_message = MessageContent(
            text=f"✍️ {_('email_from')}: {decoded_sender}",
            send_notification=False,
        )
        messages.append(from_message)

        # 3. CC information (if exists)
        if email_data.get("cc"):
            cc_message = MessageContent(
                text=f"👥 {_('email_cc')}: {email_data['cc']}",
                send_notification=False,
            )
            messages.append(cc_message)

        # 4. BCC information (if exists)
        if email_data.get("bcc"):
            bcc_message = MessageContent(
                text=f"🔒 {_('email_bcc')}: {email_data['bcc']}",
                send_notification=False,
            )
            messages.append(bcc_message)

        # 5. Email summary or content
        processed_content = self.get_processed_email_content(email_data)
        if processed_content:
            unsubscribe_urls = []
            body_html = email_data.get("body_html", "")
            if body_html and body_html.strip():
                unsubscribe_urls = extract_unsubscribe_urls(
                    body_html, default_language=os.getenv("DEFAULT_LANGUAGE", "en_US")
                )

            summary = summarize_email(processed_content, extra_urls=unsubscribe_urls)
            if summary is not None:
                # Use enhanced email summary format
                formatted_summary = format_enhanced_email_summary(summary)
                summary_header = f"<b>{_('email_summary')}:</b>\n"
                summary_message = MessageContent(
                    text=f"{summary_header}{formatted_summary}",
                    parse_mode="HTML",
                    send_notification=True,
                    urls=summary.get("urls", []),
                )
                messages.append(summary_message)
            else:
                # If summary failed, send processed original content
                max_length = 4000  # Telegram message length limit
                if len(processed_content) > max_length:
                    truncated_content = (
                        processed_content[:max_length]
                        + f"...\n\n{_('content_truncated')}"
                    )
                else:
                    truncated_content = processed_content

                content_message = MessageContent(
                    text=truncated_content,
                    send_notification=True,
                    urls=unsubscribe_urls,
                )
                messages.append(content_message)
        else:
            # If no usable email content, send notification message
            no_content_message = MessageContent(
                text=f"📧 {_('email_content_unavailable')}",
                send_notification=True,
            )
            messages.append(no_content_message)

        return messages

    def prepare_email_files(self, email_data: Dict[str, Any]) -> List[FileContent]:
        """
        Prepare email file content for sending

        Args:
            email_data: Email data dictionary

        Returns:
            List[FileContent]: List of prepared file content
        """
        files = []

        # Send original HTML file (if exists)
        if email_data.get("body_html"):
            # Clean subject for use as filename
            subject = email_data["subject"]
            clean_subject = re.sub(
                r"^(?i)(re|fw|fwd|回复|转发)[:：]\s*", "", subject.strip()
            )
            clean_subject = clean_subject.strip() or _("no_subject")
            sanitized_subject = re.sub(r'[\\/*?:"<>|]', "_", clean_subject)[:50]
            html_filename = f"{sanitized_subject}.html"

            html_file = FileContent(
                content=email_data["body_html"],
                filename=html_filename,
                send_notification=False,
            )
            files.append(html_file)

        return files

    def prepare_email_attachments(
        self, email_data: Dict[str, Any]
    ) -> List[AttachmentContent]:
        """
        Prepare email attachment content for sending

        Args:
            email_data: Email data dictionary

        Returns:
            List[AttachmentContent]: List of prepared attachment content
        """
        attachments = []

        # Process email attachments
        if email_data.get("attachments"):
            for attachment in email_data["attachments"]:
                # Decode filename
                encoded_filename = attachment["filename"]
                decoded_filename = self.decode_mime_filename(encoded_filename)

                attachment_content = AttachmentContent(
                    data=attachment["data"],
                    filename=decoded_filename,
                )
                attachments.append(attachment_content)

        return attachments

    async def send_attachments(
        self, chat_id: int, thread_id: int, attachments: List[Dict[str, Any]]
    ) -> bool:
        """
        Send all attachments as individual messages

        Args:
            chat_id: Telegram chat ID
            thread_id: Thread ID for forum topics
            attachments: List of attachment data dictionaries

        Returns:
            bool: True if all attachments were sent successfully, False otherwise
        """
        if not attachments:
            return True

        try:
            # Send each attachment without header message
            for attachment in attachments:
                await self.send_attachment(chat_id, thread_id, attachment)

            return True
        except Exception as e:
            logger.error(f"Error sending attachments: {e}")
            return False

    async def send_email_to_telegram(self, email_data: Dict[str, Any]) -> bool:
        """
        Send email to Telegram chat using atomic operations

        Args:
            email_data: Email data dictionary

        Returns:
            bool: True if successful, False if failed
        """
        try:
            # 1. Get account information
            account_id = email_data["email_account"]
            account_manager = AccountManager()
            account = account_manager.get_account(id=account_id)
            if not account:
                logger.error(f"Account not found for ID: {account_id}")
                return False

            group_id = account["tg_group_id"]
            if not group_id:
                logger.error(
                    f"No Telegram group ID configured for account: {account_id}"
                )
                return False

            # 2. Clean email subject (remove Re: prefix)
            subject = email_data["subject"]
            clean_subject = re.sub(
                r"^(?i)(re|fw|fwd|回复|转发)[:：]\s*", "", subject.strip()
            )

            # 2.1 Prefer threading by headers (In-Reply-To / References) if present.
            thread_id_hint = self.db_manager.find_thread_id_for_reply_headers(
                account_id=account_id,
                in_reply_to=email_data.get("in_reply_to"),
                references_header=email_data.get("references_header"),
            )

            # 3. Prepare all content to be sent
            logger.info(f"Preparing email content for subject: {clean_subject}")

            # Prepare message content
            messages = self.prepare_email_messages(email_data)
            logger.info(f"Prepared {len(messages)} messages")

            # Prepare file content
            files = self.prepare_email_files(email_data)
            logger.info(f"Prepared {len(files)} files")

            # Prepare attachment content
            attachments = self.prepare_email_attachments(email_data)
            logger.info(f"Prepared {len(attachments)} attachments")

            # 4. Use atomic sender to send all content
            atomic_sender = AtomicEmailSender(self)
            success = await atomic_sender.send_email_atomically(
                chat_id=group_id,
                topic_title=clean_subject,
                messages=messages,
                files=files,
                attachments=attachments,
                email_id=email_data["id"],
                account_id=account_id,
                thread_id_hint=thread_id_hint,
            )

            if success:
                logger.info(f"Successfully sent email to Telegram: {clean_subject}")
                try:
                    if atomic_sender.thread_id:
                        await self._send_email_actions(
                            chat_id=group_id,
                            thread_id=atomic_sender.thread_id,
                            email_data=email_data,
                        )
                except Exception as e:
                    logger.error(f"Failed to send action buttons: {e}")
                return True
            else:
                logger.error(f"Failed to send email to Telegram: {clean_subject}")
                return False

        except Exception as e:
            logger.error(f"Error sending email to Telegram: {e}")
            return False

    async def _send_email_actions(
        self, *, chat_id: int, thread_id: int, email_data: Dict[str, Any]
    ) -> None:
        """
        Send action buttons (Reply/Forward + optional alias suggestion) into the email topic.
        """
        email_id = email_data["id"]
        account_id = email_data["email_account"]

        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text=f"↩️ {_('reply')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"email:reply:{email_id}:{thread_id}".encode("utf-8")
                    ),
                ),
                InlineKeyboardButton(
                    text=f"➡️ {_('forward')}",
                    type=InlineKeyboardButtonTypeCallback(
                        data=f"email:forward:{email_id}:{thread_id}".encode("utf-8")
                    ),
                ),
            ]
        ]

        # Optional: identity suggestion based on Delivered-To (+ plus-addressing).
        candidates: list[str] = []
        delivered_to = email_data.get("delivered_to")
        if delivered_to:
            try:
                candidates = json.loads(delivered_to) or []
            except Exception:
                candidates = []

        identities = self.db_manager.list_account_identities(account_id=account_id)
        identity_emails = {i["from_email"] for i in identities}
        suggestion_email = suggest_identity(
            candidates=candidates, identity_emails=identity_emails
        )
        if suggestion_email:
            suggestion = self.db_manager.upsert_identity_suggestion(
                account_id=account_id,
                suggested_email=suggestion_email,
                source_delivered_to=(candidates[0] if candidates else None),
                email_id=email_id,
            )
            if suggestion.get("status") == "pending":
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"➕ {_('add_identity')} {suggestion_email}",
                            type=InlineKeyboardButtonTypeCallback(
                                data=f"id_suggest:add:{suggestion['id']}".encode("utf-8")
                            ),
                        ),
                        InlineKeyboardButton(
                            text=f"🙈 {_('ignore')}",
                            type=InlineKeyboardButtonTypeCallback(
                                data=f"id_suggest:ignore:{suggestion['id']}".encode(
                                    "utf-8"
                                )
                            ),
                        ),
                    ]
                )

        await self.bot_client.api.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            input_message_content=InputMessageText(
                text=FormattedText(text=f"⚡ {_('actions')}", entities=[])
            ),
            reply_markup=ReplyMarkupInlineKeyboard(rows=rows),
            options=MessageSendOptions(
                paid_message_star_count=0,
                sending_id=0,
                disable_notification=True,
                from_background=True,
            ),
        )

    async def send_email_to_telegram_legacy(self, email_data: Dict[str, Any]) -> bool:
        return await _send_email_to_telegram_legacy(self, email_data)
