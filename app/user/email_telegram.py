import os
import re
import html
from typing import Dict, List, Optional, Any
import tempfile
import email.header
import base64
import asyncio
from dataclasses import dataclass
from functools import wraps

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
    extract_unsubscribe_urls,
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


def retry_on_telegram_error(max_retries: int = 3, delay: float = 1.0):
    """
    Decorator to retry Telegram API calls on failure

    Args:
        max_retries: Maximum number of retry attempts
        delay: Delay between retries in seconds
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    result = await func(*args, **kwargs)
                    if result is not None:  # Success
                        if attempt > 0:
                            logger.info(
                                f"Successfully sent after {attempt} retries: {func.__name__}"
                            )
                        return result
                    else:
                        # Result is None, treat as failure
                        if attempt < max_retries:
                            logger.warning(
                                f"Attempt {attempt + 1} failed for {func.__name__}, retrying in {delay}s..."
                            )
                            await asyncio.sleep(
                                delay * (attempt + 1)
                            )  # Exponential backoff
                        else:
                            logger.error(
                                f"All {max_retries + 1} attempts failed for {func.__name__}"
                            )
                            return None
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"Attempt {attempt + 1} failed for {func.__name__} with error: {e}, retrying in {delay}s..."
                        )
                        await asyncio.sleep(
                            delay * (attempt + 1)
                        )  # Exponential backoff
                    else:
                        logger.error(
                            f"All {max_retries + 1} attempts failed for {func.__name__} with error: {e}"
                        )
                        raise last_exception
            return None

        return wrapper

    return decorator


@dataclass
class MessageContent:
    """Data class for storing message content to be sent"""

    text: str
    parse_mode: Optional[str] = None
    send_notification: bool = True
    urls: Optional[List[Dict]] = None


@dataclass
class PreparedMessageContent:
    """Data class for storing prepared FormattedText to be sent"""

    formatted_text: FormattedText
    send_notification: bool = True
    urls: Optional[List[Dict]] = None


@dataclass
class FileContent:
    """Data class for storing file content to be sent"""

    content: str
    filename: str
    send_notification: bool = False


@dataclass
class AttachmentContent:
    """Data class for storing attachment content to be sent"""

    data: bytes
    filename: str


class AtomicEmailSender:
    """Atomic email sender that ensures topic creation and message sending are atomic operations"""

    def __init__(self, email_sender: "EmailTelegramSender"):
        self.email_sender = email_sender
        self.created_topic_id: Optional[int] = None
        self.sent_messages: List[Message] = []
        self.db_updated = False

    async def send_email_atomically(
        self,
        chat_id: int,
        topic_title: str,
        messages: List[MessageContent],
        files: List[FileContent],
        attachments: List[AttachmentContent],
        email_id: int,
        account_id: int,
    ) -> bool:
        """
        Atomically send an email to a Telegram chat

        This method ensures that topic creation happens after all content is prepared,
        and all message sending operations include retry mechanisms.

        Args:
            chat_id: Telegram chat ID
            topic_title: Topic title for the forum
            messages: List of message content to be sent
            files: List of file content to be sent
            attachments: List of attachment content to be sent
            email_id: Email database ID
            account_id: Account database ID

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            logger.info(f"Starting atomic email send for topic: {topic_title}")

            # 0. Parse/validate all message text BEFORE creating topic.
            prepared_messages = await self._prepare_formatted_messages(messages)

            # 1. First check if a thread already exists
            thread_id = await self.email_sender.get_thread_id_by_subject(
                topic_title, account_id
            )

            # 2. If no thread exists, create a new one AFTER content is prepared
            if not thread_id:
                logger.info(
                    f"No existing thread found, creating new topic: {topic_title}"
                )
                thread_id = await self._create_forum_topic_with_retry(
                    chat_id, topic_title
                )
                if not thread_id:
                    logger.error("Failed to create forum topic after retries")
                    return False
                self.created_topic_id = thread_id

            # 3. Update database with thread ID
            if not self.email_sender.db_manager.update_thread_id_in_db(
                email_id, thread_id
            ):
                logger.error("Failed to update thread ID in database")
                await self._rollback()
                return False
            self.db_updated = True

            # 4. Send all messages in order with retry mechanism
            logger.info(f"Sending {len(prepared_messages)} messages to thread {thread_id}")
            for i, message in enumerate(prepared_messages):
                sent_message = await self._send_formatted_text_message_with_retry(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    formatted_text=message.formatted_text,
                    send_notification=message.send_notification,
                    urls=message.urls,
                )
                if not sent_message:
                    logger.error(
                        f"Failed to send message {i+1}/{len(prepared_messages)}: {message.formatted_text.text[:50]}..."
                    )
                    await self._rollback()
                    return False
                self.sent_messages.append(sent_message)

            # 5. Send all files with retry mechanism
            logger.info(f"Sending {len(files)} files to thread {thread_id}")
            for i, file in enumerate(files):
                sent_message = await self._send_html_as_file_with_retry(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    content=file.content,
                    filename=file.filename,
                    send_notification=file.send_notification,
                )
                if not sent_message:
                    logger.error(
                        f"Failed to send file {i+1}/{len(files)}: {file.filename}"
                    )
                    await self._rollback()
                    return False
                self.sent_messages.append(sent_message)

            # 6. Send all attachments with retry mechanism
            logger.info(f"Sending {len(attachments)} attachments to thread {thread_id}")
            for i, attachment in enumerate(attachments):
                sent_message = await self._send_attachment_with_retry(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    attachment={
                        "data": attachment.data,
                        "filename": attachment.filename,
                    },
                )
                if not sent_message:
                    logger.error(
                        f"Failed to send attachment {i+1}/{len(attachments)}: {attachment.filename}"
                    )
                    await self._rollback()
                    return False
                self.sent_messages.append(sent_message)

            logger.info(
                f"Successfully sent email atomically. Topic: {topic_title}, Messages: {len(self.sent_messages)}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in atomic email sending: {e}")
            await self._rollback()
            return False

    @retry_on_telegram_error(max_retries=3, delay=1.0)
    async def _create_forum_topic_with_retry(
        self, chat_id: int, title: str
    ) -> Optional[int]:
        """Create forum topic with retry mechanism"""
        return await self.email_sender.create_forum_topic(chat_id, title)

    @retry_on_telegram_error(max_retries=3, delay=1.0)
    async def _send_formatted_text_message_with_retry(
        self,
        chat_id: int,
        thread_id: int,
        formatted_text: FormattedText,
        send_notification: bool = True,
        urls: Optional[List[Dict]] = None,
    ) -> Optional[Message]:
        """Send pre-parsed text message with retry mechanism"""
        return await self.email_sender.send_formatted_text_message(
            chat_id=chat_id,
            thread_id=thread_id,
            formatted_text=formatted_text,
            send_notification=send_notification,
            urls=urls,
        )

    @retry_on_telegram_error(max_retries=3, delay=1.0)
    async def _send_html_as_file_with_retry(
        self,
        chat_id: int,
        thread_id: int,
        content: str,
        filename: str,
        send_notification: bool = False,
    ) -> Optional[Message]:
        """Send HTML file with retry mechanism"""
        return await self.email_sender.send_html_as_file(
            chat_id=chat_id,
            thread_id=thread_id,
            content=content,
            filename=filename,
            send_notification=send_notification,
        )

    @retry_on_telegram_error(max_retries=3, delay=1.0)
    async def _send_attachment_with_retry(
        self,
        chat_id: int,
        thread_id: int,
        attachment: Dict[str, Any],
    ) -> Optional[Message]:
        """Send attachment with retry mechanism"""
        return await self.email_sender.send_attachment(
            chat_id=chat_id,
            thread_id=thread_id,
            attachment=attachment,
        )

    async def _rollback(self):
        """
        Rollback all operations if any step fails

        This is a simple implementation that only logs the actions.
        Actual rollback in Telegram might not be possible.
        """
        try:
            # Delete sent messages
            # Telegram Bot API typically does not allow deleting messages
            if self.sent_messages:
                logger.warning(
                    f"Rollback: {len(self.sent_messages)} messages were sent but operation failed"
                )

            # If a new topic was created, try to delete it (Telegram API might not support deleting topics)
            if self.created_topic_id:
                logger.warning(
                    f"Rollback: Created topic {self.created_topic_id} but operation failed"
                )
                # Here you can try to delete the topic, but Telegram API might not support it
                # await self.email_sender.bot_client.api.delete_forum_topic(...)

            # Rollback database update
            if self.db_updated:
                # Here you can implement the rollback logic for the database
                logger.warning("Rollback: Database was updated but operation failed")

        except Exception as e:
            logger.error(f"Error during rollback: {e}")

    async def _prepare_formatted_messages(
        self, messages: List[MessageContent]
    ) -> List["PreparedMessageContent"]:
        prepared_messages: List[PreparedMessageContent] = []
        for message in messages:
            formatted_text = await self._parse_message_text(
                text=message.text,
                parse_mode=message.parse_mode,
            )
            prepared_messages.append(
                PreparedMessageContent(
                    formatted_text=formatted_text,
                    send_notification=message.send_notification,
                    urls=message.urls,
                )
            )
        return prepared_messages

    async def _parse_message_text(
        self, *, text: str, parse_mode: Optional[str]
    ) -> FormattedText:
        try:
            return await self.email_sender.str_to_formatted(text, parse_mode)
        except Exception as e:
            fallback_text = self._fallback_plain_text(text, parse_mode)
            logger.warning(
                "Failed to parse message text, sending plain text instead "
                f"(parse_mode={parse_mode}): {e}"
            )
            return FormattedText(text=fallback_text, entities=[])

    def _fallback_plain_text(self, text: str, parse_mode: Optional[str]) -> str:
        if isinstance(parse_mode, str) and parse_mode.lower() == "html":
            # Convert common HTML newlines before stripping tags.
            plain = re.sub(r"<br\\s*/?>", "\n", text, flags=re.IGNORECASE)
            plain = re.sub(r"<[^>]+>", "", plain)
            return html.unescape(plain).strip()
        return text


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
        title_message = MessageContent(
            text=f"*{email_data['subject']}*",
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
            )

            if success:
                logger.info(f"Successfully sent email to Telegram: {clean_subject}")
                return True
            else:
                logger.error(f"Failed to send email to Telegram: {clean_subject}")
                return False

        except Exception as e:
            logger.error(f"Error sending email to Telegram: {e}")
            return False

    async def send_email_to_telegram_legacy(self, email_data: Dict[str, Any]) -> bool:
        """
        Legacy email sending method (kept as backup)

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
            await self.send_text_message(
                chat_id=group_id,
                text=f"*{email_data['subject']}*",
                thread_id=thread_id,
                parse_mode="Markdown",
                send_notification=False,
            )

            # 3. Send email headers - From
            await self.send_text_message(
                chat_id=group_id,
                text=f"✍️ {_('email_from')}: {decoded_sender}",
                thread_id=thread_id,
                send_notification=False,
            )

            # 4. CC (if exists)
            if email_data.get("cc"):
                await self.send_text_message(
                    chat_id=group_id,
                    text=f"👥 {_('email_cc')}: {email_data['cc']}",
                    thread_id=thread_id,
                    send_notification=False,
                )

            # 5. BCC (if exists)
            if email_data.get("bcc"):
                await self.send_text_message(
                    chat_id=group_id,
                    text=f"🔒 {_('email_bcc')}: {email_data['bcc']}",
                    thread_id=thread_id,
                    send_notification=False,
                )

            # 6. Email Summary - Use enhanced processing logic, prioritize HTML, fallback to plain text
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
                    # Use new formatting function to display enhanced email summary
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
                    # If summary failed, send processed original content
                    # Limit content length to avoid overly long messages
                    max_length = 4000  # Telegram message length limit
                    if len(processed_content) > max_length:
                        truncated_content = (
                            processed_content[:max_length]
                            + f"...\n\n{_('content_truncated')}"
                        )
                    else:
                        truncated_content = processed_content

                    await self.send_text_message(
                        chat_id=group_id,
                        text=truncated_content,
                        urls=unsubscribe_urls,
                        thread_id=thread_id,
                        send_notification=True,
                    )
            else:
                # If no usable email content, send notification message
                await self.send_text_message(
                    chat_id=group_id,
                    text=f"📧 {_('email_content_unavailable')}",
                    thread_id=thread_id,
                    send_notification=True,
                )

            # 7. Send original HTML as a file attachment if needed
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

            # 8. Send attachments if any
            if email_data.get("attachments"):
                await self.send_attachments(
                    group_id, thread_id, email_data["attachments"]
                )

            return True

        except Exception as e:
            logger.error(f"Error sending email to Telegram: {e}")
            return False
