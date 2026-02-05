import sqlite3
from typing import Any, Dict, List

from app.utils import Logger

logger = Logger().get_logger(__name__)


class TopicTrackingMixin:
    def get_chat_event_cursor(self, chat_id: int) -> int:
        """
        Get the last processed forum event_id for a chat.

        Args:
            chat_id: Telegram group chat ID.

        Returns:
            int: last processed event_id; 0 if not set.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_forum_event_id FROM chat_event_cursors WHERE chat_id = ?",
                (int(chat_id),),
            )
            row = cursor.fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"Error getting chat event cursor for chat {chat_id}: {e}")
            return 0

    def set_chat_event_cursor(self, chat_id: int, event_id: int) -> bool:
        """
        Upsert the last processed forum event_id for a chat.

        Args:
            chat_id: Telegram group chat ID.
            event_id: Latest processed event_id.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO chat_event_cursors (chat_id, last_forum_event_id)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET last_forum_event_id = excluded.last_forum_event_id
                """,
                (int(chat_id), int(event_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error setting chat event cursor for chat {chat_id} to {event_id}: {e}"
            )
            return False

    def upsert_deleted_topic(
        self, chat_id: int, thread_id: str, event_id: int, deleted_at: int
    ) -> bool:
        """
        Record a deleted topic so it can be processed reliably even after restarts.

        Args:
            chat_id: Telegram group chat ID.
            thread_id: Telegram forum topic thread ID.
            event_id: Chat event log event_id that recorded the deletion.
            deleted_at: Unix timestamp of deletion event.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO deleted_topics (chat_id, thread_id, event_id, deleted_at, processed_at, attempts, last_error)
                VALUES (?, ?, ?, ?, NULL, 0, NULL)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    deleted_at = excluded.deleted_at
                """,
                (int(chat_id), str(thread_id), int(event_id), int(deleted_at)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error upserting deleted topic (chat_id={chat_id}, thread_id={thread_id}): {e}"
            )
            return False

    def list_pending_deleted_topics(self, chat_id: int) -> List[Dict[str, Any]]:
        """
        List topics deleted in Telegram that still need IMAP deletion processing.

        Args:
            chat_id: Telegram group chat ID.

        Returns:
            List[Dict[str, Any]]: Rows containing thread_id, attempts, last_error, deleted_at.
        """
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT chat_id, thread_id, attempts, last_error, deleted_at
                FROM deleted_topics
                WHERE chat_id = ? AND processed_at IS NULL
                ORDER BY deleted_at ASC
                """,
                (int(chat_id),),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Error listing pending deleted topics for chat {chat_id}: {e}")
            return []

    def mark_deleted_topic_processed(self, chat_id: int, thread_id: str) -> bool:
        """
        Mark a deleted topic as processed.

        Args:
            chat_id: Telegram group chat ID.
            thread_id: Telegram thread ID.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE deleted_topics
                SET processed_at = strftime('%s','now'), last_error = NULL
                WHERE chat_id = ? AND thread_id = ?
                """,
                (int(chat_id), str(thread_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error marking deleted topic processed (chat_id={chat_id}, thread_id={thread_id}): {e}"
            )
            return False

    def record_deleted_topic_failure(
        self, chat_id: int, thread_id: str, error: str
    ) -> bool:
        """
        Record a failure while processing a deleted topic.

        Args:
            chat_id: Telegram group chat ID.
            thread_id: Telegram thread ID.
            error: Error string for debugging.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE deleted_topics
                SET attempts = attempts + 1, last_error = ?
                WHERE chat_id = ? AND thread_id = ? AND processed_at IS NULL
                """,
                (str(error), int(chat_id), str(thread_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(
                f"Error recording deleted topic failure (chat_id={chat_id}, thread_id={thread_id}): {e}"
            )
            return False

