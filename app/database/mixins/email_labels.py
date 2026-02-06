import sqlite3
import time
from typing import Any, Dict, List, Optional

from app.utils import Logger

logger = Logger().get_logger(__name__)


class EmailLabelsMixin:
    def _normalize_account_ids(self, account_ids: Optional[List[int]]) -> list[int]:
        if account_ids is None:
            return []
        normalized: list[int] = []
        seen: set[int] = set()
        for item in account_ids:
            try:
                value = int(item)
            except Exception:
                continue
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def update_email_llm_labels(
        self,
        *,
        email_id: int,
        category: str,
        priority: str,
        confidence: Optional[float] = None,
        labeled_at: Optional[int] = None,
    ) -> bool:
        try:
            ts = int(labeled_at) if labeled_at is not None else int(time.time())
            conf_value: Optional[float]
            if confidence is None:
                conf_value = None
            else:
                conf_value = float(confidence)
                if conf_value < 0:
                    conf_value = 0.0
                elif conf_value > 1:
                    conf_value = 1.0

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE emails
                SET llm_category = ?, llm_priority = ?, llm_confidence = ?, llm_labeled_at = ?
                WHERE id = ?
                """,
                (
                    (category or "").strip().lower() or "other",
                    (priority or "").strip().lower() or "medium",
                    conf_value,
                    ts,
                    int(email_id),
                ),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating email LLM labels: {e}")
            return False

    def list_labeled_emails(
        self,
        *,
        category: str,
        days: int = 7,
        account_ids: Optional[List[int]] = None,
        limit: int = 5,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_accounts = self._normalize_account_ids(account_ids)
        if account_ids is not None and not normalized_accounts:
            return []

        try:
            days_int = max(1, int(days))
        except Exception:
            days_int = 7
        cutoff = int(time.time()) - (days_int * 24 * 3600)

        try:
            limit_int = max(1, int(limit))
        except Exception:
            limit_int = 5
        try:
            offset_int = max(0, int(offset))
        except Exception:
            offset_int = 0

        where = [
            "e.llm_category = ?",
            "e.llm_labeled_at IS NOT NULL",
            "e.llm_labeled_at >= ?",
            "COALESCE(e.telegram_thread_id, '') <> ''",
        ]
        params: list[Any] = [(category or "").strip().lower(), cutoff]

        if normalized_accounts:
            placeholders = ",".join(["?"] * len(normalized_accounts))
            where.append(f"e.email_account IN ({placeholders})")
            params.extend(normalized_accounts)

        params.extend([limit_int, offset_int])
        sql = f"""
            SELECT
              e.id,
              e.email_account,
              e.subject,
              e.sender,
              e.email_date,
              e.mailbox,
              e.telegram_thread_id,
              e.llm_category,
              e.llm_priority,
              e.llm_confidence,
              e.llm_labeled_at,
              a.email AS account_email
            FROM emails e
            LEFT JOIN accounts a ON a.id = e.email_account
            WHERE {" AND ".join(where)}
            ORDER BY e.llm_labeled_at DESC, e.id DESC
            LIMIT ? OFFSET ?
        """

        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Error listing labeled emails: {e}")
            return []

    def count_labeled_emails(
        self,
        *,
        category: str,
        days: int = 7,
        account_ids: Optional[List[int]] = None,
    ) -> int:
        normalized_accounts = self._normalize_account_ids(account_ids)
        if account_ids is not None and not normalized_accounts:
            return 0

        try:
            days_int = max(1, int(days))
        except Exception:
            days_int = 7
        cutoff = int(time.time()) - (days_int * 24 * 3600)

        where = [
            "llm_category = ?",
            "llm_labeled_at IS NOT NULL",
            "llm_labeled_at >= ?",
            "COALESCE(telegram_thread_id, '') <> ''",
        ]
        params: list[Any] = [(category or "").strip().lower(), cutoff]

        if normalized_accounts:
            placeholders = ",".join(["?"] * len(normalized_accounts))
            where.append(f"email_account IN ({placeholders})")
            params.extend(normalized_accounts)

        sql = f"SELECT COUNT(1) FROM emails WHERE {' AND '.join(where)}"
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            row = cursor.fetchone()
            conn.close()
            return int((row or [0])[0] or 0)
        except Exception as e:
            logger.error(f"Error counting labeled emails: {e}")
            return 0

    def count_labeled_emails_by_category(
        self,
        *,
        days: int = 7,
        account_ids: Optional[List[int]] = None,
    ) -> Dict[str, int]:
        normalized_accounts = self._normalize_account_ids(account_ids)
        if account_ids is not None and not normalized_accounts:
            return {}

        try:
            days_int = max(1, int(days))
        except Exception:
            days_int = 7
        cutoff = int(time.time()) - (days_int * 24 * 3600)

        where = [
            "llm_category IS NOT NULL",
            "llm_category <> ''",
            "llm_labeled_at IS NOT NULL",
            "llm_labeled_at >= ?",
            "COALESCE(telegram_thread_id, '') <> ''",
        ]
        params: list[Any] = [cutoff]
        if normalized_accounts:
            placeholders = ",".join(["?"] * len(normalized_accounts))
            where.append(f"email_account IN ({placeholders})")
            params.extend(normalized_accounts)

        sql = f"""
            SELECT llm_category, COUNT(1)
            FROM emails
            WHERE {" AND ".join(where)}
            GROUP BY llm_category
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            conn.close()
            result: dict[str, int] = {}
            for category, count in rows:
                key = str(category or "").strip().lower()
                if not key:
                    continue
                result[key] = int(count or 0)
            return result
        except Exception as e:
            logger.error(f"Error counting labeled emails by category: {e}")
            return {}
