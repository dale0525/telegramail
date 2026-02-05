import asyncio
import html
import re
from dataclasses import dataclass
from functools import wraps
from typing import Any, Dict, List, Optional

from aiotdlib.api import FormattedText, Message

from app.utils import Logger

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
        self.thread_id: Optional[int] = None
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
        thread_id_hint: Optional[int] = None,
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

            # 1. Resolve thread id:
            #    - prefer explicit hint (reply threading), then
            #    - fall back to subject-based grouping.
            thread_id = int(thread_id_hint) if thread_id_hint else None
            if not thread_id:
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
            self.thread_id = thread_id

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
