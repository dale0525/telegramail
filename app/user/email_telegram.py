import os
import re
from typing import Dict, List, Optional, Any
import tempfile
import email.header
import base64

from app.bot.bot_client import BotClient
from app.i18n import _
from app.utils import Logger
from app.database import DBManager
from app.user.user_client import UserClient
from app.email_utils import (
    summarize_email,
    format_enhanced_email_summary,
    AccountManager,
    clean_html_content,
)
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
    InlineKeyboardButtonTypeUrl,
    ReplyMarkupInlineKeyboard,
)
from aiotdlib.api import MessageSendOptions

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
            send_notification: Whethere to send telegram notification

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
                        + '\\n<meta charset="UTF-8">'
                        + content[inject_pos:]
                    )
                else:
                    # If no <head> tag, prepend to the whole content
                    content = '<meta charset="UTF-8">\\n' + content

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
        获取处理后的邮件内容，实现降级处理机制

        Args:
            email_data: 邮件数据字典

        Returns:
            str: 处理后的邮件内容，优先使用HTML版本，降级到纯文本版本
        """
        # 优先使用body_html（如果存在且非空）
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

        # 如果body_html不存在、为空或处理失败，则使用body_text作为备选
        body_text = email_data.get("body_text", "")
        if body_text and body_text.strip():
            logger.info("Using text content for email processing")
            return body_text.strip()

        # 如果两者都不可用，返回空字符串
        logger.warning("No usable email content found (both HTML and text are empty)")
        return ""

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
        Send email to a Telegram chat

        Args:
            email_data: Email data dictionary

        Returns:
            bool: True if email was sent successfully, False otherwise
        """
        try:
            account_id = email_data["email_account"]
            account_manager = AccountManager()
            account = account_manager.get_account(id=account_id)
            group_id = account["tg_group_id"]

            # Clean subject (remove Re: prefix)
            subject = email_data["subject"]
            clean_subject = re.sub(
                r"^(?i)(re|fw|fwd|回复|转发)[:：]\s*", "", subject.strip()
            )

            # Decode sender name
            original_sender = email_data.get("sender", "")
            decoded_sender = self.decode_mime_header_value(original_sender)

            # Check for existing thread ID
            thread_id = await self.get_thread_id_by_subject(clean_subject, account_id)

            # If no thread exists, create a new forum topic
            if not thread_id:
                thread_id = await self.create_forum_topic(group_id, clean_subject)
                if not thread_id:
                    logger.error("Failed to create forum topic")
                    return False

            # Update database with thread ID
            self.db_manager.update_thread_id_in_db(email_data["id"], thread_id)

            # 2. Send email title
            # Title
            await self.send_text_message(
                chat_id=group_id,
                text=f"*{email_data['subject']}*",
                thread_id=thread_id,
                parse_mode="Markdown",
                send_notification=False,
            )

            # 3. Send email headers
            # From
            await self.send_text_message(
                chat_id=group_id,
                text=f"✍️ {_('email_from')}: {decoded_sender}",
                thread_id=thread_id,
                send_notification=False,
            )

            # # To
            # await self.send_text_message(
            #     group_id, f"<b>To:</b> {email_data['recipient']}", thread_id
            # )

            # # Date
            # await self.send_text_message(
            #     group_id, f"<b>Date:</b> {email_data['date']}", thread_id
            # )

            # 4. CC (if exists)
            if email_data.get("cc"):
                await self.send_text_message(
                    chat_id=group_id,
                    text=f"👥 {_('email_cc')}: {email_data['cc']}",
                    thread_id=thread_id,
                    send_notification=False,
                )

            # 4. BCC (if exists)
            if email_data.get("bcc"):
                await self.send_text_message(
                    chat_id=group_id,
                    text=f"🔒 {_('email_bcc')}: {email_data['bcc']}",
                    thread_id=thread_id,
                    send_notification=False,
                )

            # 5. Email Summary - 使用增强的处理逻辑，优先HTML，降级到纯文本
            processed_content = self.get_processed_email_content(email_data)
            if processed_content:
                summary = summarize_email(processed_content)
                if summary is not None:
                    # 使用新的格式化函数来显示增强的邮件摘要
                    formatted_summary = format_enhanced_email_summary(summary)
                    summary_header = f"<b>{_('email_summary')}:</b>\n"

                    await self.send_text_message(
                        chat_id=group_id,
                        text=f"{summary_header}{formatted_summary}",
                        urls=summary.get("urls", []),
                        thread_id=thread_id,
                        parse_mode="HTML",
                        send_notification=True,
                    )
                else:
                    # 如果总结失败，发送处理后的原始内容
                    # 限制内容长度以避免消息过长
                    max_length = 4000  # Telegram消息长度限制
                    if len(processed_content) > max_length:
                        truncated_content = (
                            processed_content[:max_length] + "...\n\n[内容已截断]"
                        )
                    else:
                        truncated_content = processed_content

                    await self.send_text_message(
                        chat_id=group_id,
                        text=truncated_content,
                        thread_id=thread_id,
                        send_notification=True,
                    )
            else:
                # 如果没有可用的邮件内容，发送提示信息
                await self.send_text_message(
                    chat_id=group_id,
                    text="📧 邮件内容无法显示",
                    thread_id=thread_id,
                    send_notification=True,
                )

            # 6. Send original HTML as a file attachment if needed
            if email_data.get("body_html"):
                # Generate a filename based on the email subject
                sanitized_subject = re.sub(r'[\\/*?:"<>|]', "_", clean_subject)[
                    :50
                ]  # Sanitize and limit length
                html_filename = f"{sanitized_subject}.html"
                await self.send_html_as_file(
                    chat_id=group_id,
                    thread_id=thread_id,
                    content=email_data["body_html"],
                    filename=html_filename,
                    send_notification=False,
                )

            # 7. Send attachments if any
            if email_data.get("attachments"):
                await self.send_attachments(
                    group_id, thread_id, email_data["attachments"]
                )

            return True

        except Exception as e:
            logger.error(f"Error sending email to Telegram: {e}")
            return False
