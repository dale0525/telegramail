import sqlite3
import time
from typing import Any, Dict, List, Optional

from app.utils import Logger

logger = Logger().get_logger(__name__)


class DraftsMixin:
    # --- Drafts ---

    def create_draft(
        self,
        *,
        account_id: int,
        chat_id: int,
        thread_id: int,
        draft_type: str,
        from_identity_email: str,
    ) -> int:
        now = int(time.time())
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO drafts
              (account_id, chat_id, thread_id, draft_type, from_identity_email, status, created_at, updated_at)
            VALUES
              (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                int(account_id),
                int(chat_id),
                int(thread_id),
                (draft_type or "compose").strip(),
                (from_identity_email or "").strip().lower(),
                now,
                now,
            ),
        )
        draft_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return int(draft_id)

    def get_active_draft(
        self, *, chat_id: int, thread_id: int
    ) -> Optional[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM drafts
                WHERE chat_id = ? AND thread_id = ? AND status = 'open'
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(chat_id), int(thread_id)),
            )
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting active draft: {e}")
            return None

    def update_draft(self, *, draft_id: int, updates: Dict[str, Any]) -> bool:
        if not updates:
            return True
        allowed = {
            "from_identity_email",
            "card_message_id",
            "to_addrs",
            "cc_addrs",
            "bcc_addrs",
            "subject",
            "in_reply_to",
            "references_header",
            "body_markdown",
            "status",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return True

        filtered["updated_at"] = int(time.time())

        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            set_clause = ", ".join([f"{k} = ?" for k in filtered.keys()])
            params = list(filtered.values()) + [int(draft_id)]
            cursor.execute(f"UPDATE drafts SET {set_clause} WHERE id = ?", params)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating draft: {e}")
            return False

    def append_draft_body(self, *, draft_id: int, text: str) -> bool:
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT body_markdown FROM drafts WHERE id = ?", (int(draft_id),)
            )
            row = cursor.fetchone()
            current = (row["body_markdown"] if row else "") or ""
            addition = (text or "").strip()
            if not addition:
                conn.close()
                return True

            if current.strip():
                new_body = f"{current}\n\n{addition}"
            else:
                new_body = addition

            cursor.execute(
                "UPDATE drafts SET body_markdown = ?, updated_at = ? WHERE id = ?",
                (new_body, int(time.time()), int(draft_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error appending draft body: {e}")
            return False

    def record_draft_message(
        self,
        *,
        draft_id: int,
        chat_id: int,
        thread_id: int,
        message_id: int,
        message_type: Optional[str] = None,
    ) -> bool:
        try:
            now = int(time.time())
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO draft_messages
                  (draft_id, chat_id, thread_id, message_id, message_type, created_at)
                VALUES
                  (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(draft_id),
                    int(chat_id),
                    int(thread_id),
                    int(message_id),
                    (message_type or "").strip() or None,
                    now,
                ),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error recording draft message: {e}")
            return False

    def list_draft_message_ids(self, *, draft_id: int) -> List[int]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT message_id
                FROM draft_messages
                WHERE draft_id = ?
                ORDER BY message_id ASC
                """,
                (int(draft_id),),
            )
            rows = cursor.fetchall()
            conn.close()
            ids: list[int] = []
            for row in rows:
                if not row or row[0] is None:
                    continue
                try:
                    ids.append(int(row[0]))
                except Exception:
                    continue
            return ids
        except Exception as e:
            logger.error(f"Error listing draft message ids: {e}")
            return []

    def clear_draft_messages(self, *, draft_id: int) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM draft_messages WHERE draft_id = ?",
                (int(draft_id),),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error clearing draft messages: {e}")
            return False

    def add_draft_attachment(
        self,
        *,
        draft_id: int,
        file_id: int,
        remote_id: str | None,
        file_type: str | None,
        file_name: str,
        mime_type: str | None,
        size: int | None,
    ) -> Optional[int]:
        try:
            now = int(time.time())
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO draft_attachments
                  (draft_id, file_id, remote_id, file_type, file_name, mime_type, size, created_at, updated_at)
                VALUES
                  (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(draft_id),
                    int(file_id),
                    (remote_id or "").strip() or None,
                    (file_type or "").strip() or None,
                    (file_name or "").strip(),
                    (mime_type or "").strip() or None,
                    int(size) if size is not None else None,
                    now,
                    now,
                ),
            )
            attachment_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return int(attachment_id)
        except Exception as e:
            logger.error(f"Error adding draft attachment: {e}")
            return None

    def list_draft_attachments(self, *, draft_id: int) -> List[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM draft_attachments
                WHERE draft_id = ?
                ORDER BY id ASC
                """,
                (int(draft_id),),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Error listing draft attachments: {e}")
            return []

    def delete_draft_attachment(self, *, draft_id: int, attachment_id: int) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM draft_attachments WHERE id = ? AND draft_id = ?",
                (int(attachment_id), int(draft_id)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error deleting draft attachment: {e}")
            return False

    def clear_draft_attachments(self, *, draft_id: int) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM draft_attachments WHERE draft_id = ?",
                (int(draft_id),),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error clearing draft attachments: {e}")
            return False

