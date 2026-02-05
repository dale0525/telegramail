import asyncio
import os
from typing import Set
from aiotdlib.api import (
    ChatEventLogFilters,
)
from app.database import DBManager
from app.email_utils.imap_client import IMAPClient
from app.utils import Logger, retry_on_fail
from app.cron.cron_utils import start_periodic_task
from app.email_utils import AccountManager

logger = Logger().get_logger(__name__)


@retry_on_fail(max_retries=2, retry_delay=1.0)
async def check_deleted_topics_for_group(chat_id):
    """
    Check deleted topics for a specific group
    """
    try:
        from app.user.user_client import UserClient

        db_manager = DBManager()

        last_event_id = db_manager.get_chat_event_cursor(chat_id)
        newest_seen_event_id = last_event_id
        from_event_id = 0
        try:
            request_timeout = int(os.getenv("TELEGRAM_CHAT_EVENT_LOG_TIMEOUT", "30"))
        except ValueError:
            logger.warning(
                "Invalid TELEGRAM_CHAT_EVENT_LOG_TIMEOUT value; falling back to 30s",
                exc_info=False,
            )
            request_timeout = 30
        if request_timeout <= 0:
            logger.warning(
                "TELEGRAM_CHAT_EVENT_LOG_TIMEOUT must be a positive integer; falling back to 30s",
                exc_info=False,
            )
            request_timeout = 30
        seen_event_ids: Set[int] = set()
        while True:
            events = None
            for attempt in range(1, 4):
                try:
                    events = await UserClient().client.api.get_chat_event_log(
                        chat_id=chat_id,
                        query="",
                        from_event_id=from_event_id,
                        limit=100,
                        filters=ChatEventLogFilters(forum_changes=True),
                        user_ids=[],
                        request_timeout=request_timeout * attempt,
                    )
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    if attempt >= 3:
                        logger.warning(
                            f"TDLib get_chat_event_log timed out for chat {chat_id} "
                            f"(from_event_id={from_event_id}) after {attempt} attempts; "
                            "skipping event scan until next run",
                            exc_info=False,
                        )
                        break
                    logger.warning(
                        f"TDLib get_chat_event_log timed out for chat {chat_id} "
                        f"(from_event_id={from_event_id}) attempt {attempt}/3; retrying",
                        exc_info=False,
                    )
                    await asyncio.sleep(1.0 * attempt)

            if events is None:
                break

            if not events.events:
                break

            newest_seen_event_id = max(newest_seen_event_id, int(events.events[0].id))

            reached_cursor = False
            for event in events.events:
                event_id = int(event.id)
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)

                if event_id <= last_event_id:
                    reached_cursor = True
                    break

                if event.action.ID != "chatEventForumTopicDeleted":
                    continue

                topic_info = event.action.topic_info
                thread_id = str(topic_info.message_thread_id)
                ok = db_manager.upsert_deleted_topic(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    event_id=event_id,
                    deleted_at=int(event.date),
                )
                if not ok:
                    raise RuntimeError(
                        f"Failed to persist deleted topic (chat_id={chat_id}, thread_id={thread_id})"
                    )

            if reached_cursor:
                break

            # Pagination: keep walking back in time until we reach last_event_id.
            if len(events.events) < 100:
                break

            oldest_event_id = int(events.events[-1].id)
            if oldest_event_id == from_event_id:
                # Defensive: avoid infinite loops if the API returns the same page.
                break
            from_event_id = oldest_event_id

        if newest_seen_event_id > last_event_id:
            db_manager.set_chat_event_cursor(chat_id, newest_seen_event_id)

        # Process all pending deleted topics (including ones recorded on previous runs).
        pending_topics = db_manager.list_pending_deleted_topics(chat_id)

        processed_count = 0
        for topic in pending_topics:
            thread_id = str(topic["thread_id"])

            try:
                targets_by_account = db_manager.get_deletion_targets_for_topic(
                    chat_id=chat_id, thread_id=thread_id
                )
            except Exception as db_err:
                db_manager.record_deleted_topic_failure(
                    chat_id, thread_id, f"DB error: {db_err}"
                )
                continue

            if not targets_by_account:
                # Nothing left to delete (already processed, or mapping missing). Mark processed to avoid retries.
                db_manager.mark_deleted_topic_processed(chat_id, thread_id)
                continue

            account_manager = AccountManager()
            deleted_count_in_loop = 0
            attempted_count_in_loop = 0
            all_ok = True
            for account_id, targets in targets_by_account.items():
                inbox_uids = list((targets or {}).get("inbox_uids") or [])
                outgoing_message_ids = list(
                    (targets or {}).get("outgoing_message_ids") or []
                )

                account = account_manager.get_account(id=account_id)
                if not account:
                    logger.warning(
                        f"Account with ID {account_id} not found for topic {thread_id}; cleaning local mapping"
                    )
                    # We can't delete from provider without credentials; still remove local rows so we don't
                    # keep retrying and accidentally thread future replies into a deleted topic.
                    try:
                        stub_account = {"id": int(account_id)}
                        for email_uid in inbox_uids:
                            db_manager.delete_email_by_uid(
                                stub_account, str(email_uid).strip()
                            )
                        for mid in outgoing_message_ids:
                            mid_norm = str(mid).strip()
                            if not mid_norm:
                                continue
                            db_manager.delete_email_by_uid(
                                stub_account, f"outgoing:{mid_norm}"
                            )
                    except Exception as cleanup_err:
                        logger.debug(
                            f"Failed to cleanup local mapping for missing account {account_id}: {cleanup_err}"
                        )
                        all_ok = False
                    continue

                imap_client = IMAPClient(account)

                for email_uid in inbox_uids:
                    email_uid = str(email_uid).strip()
                    if not email_uid:
                        continue
                    attempted_count_in_loop += 1
                    try:
                        logger.info(
                            f"Attempting to delete INBOX email UID {email_uid} for topic {thread_id}"
                        )
                        ok = imap_client.delete_email_by_uid(email_uid)
                        if ok:
                            deleted_count_in_loop += 1
                        else:
                            all_ok = False
                    except Exception as delete_error:
                        all_ok = False
                        logger.error(
                            f"Error deleting email UID {email_uid} for topic {thread_id}: {delete_error}"
                        )

                for message_id in outgoing_message_ids:
                    message_id = str(message_id).strip()
                    if not message_id:
                        continue
                    attempted_count_in_loop += 1
                    try:
                        logger.info(
                            f"Attempting to delete Sent email Message-ID {message_id} for topic {thread_id}"
                        )
                        ok = imap_client.delete_outgoing_email_by_message_id(message_id)
                        if ok:
                            deleted_count_in_loop += 1
                        else:
                            all_ok = False
                    except Exception as delete_error:
                        all_ok = False
                        logger.error(
                            f"Error deleting outgoing Message-ID {message_id} for topic {thread_id}: {delete_error}"
                        )

            if all_ok:
                db_manager.mark_deleted_topic_processed(chat_id, thread_id)
            else:
                db_manager.record_deleted_topic_failure(
                    chat_id, thread_id, "Failed to delete one or more emails"
                )

            if deleted_count_in_loop > 0:
                logger.info(
                    f"Deleted {deleted_count_in_loop}/{attempted_count_in_loop} emails for topic {thread_id}"
                )

            processed_count += 1

        if processed_count > 0:
            logger.info(
                f"Processed {processed_count} deleted topics for chat {chat_id}"
            )

    except Exception as e:
        logger.error(f"Error checking deleted topics for chat {chat_id}: {e}")


async def check_all_deleted_topics():
    """
    Check deleted topics for all groups
    """
    account_manager = AccountManager()
    accounts = account_manager.get_all_accounts()
    for account in accounts:
        logger.info(
            f"Checking deleted topics for group {account['tg_group_id']} (email: {account['email']})"
        )
        await check_deleted_topics_for_group(account["tg_group_id"])


def listen_to_email_deletions():
    """
    Start the email delete listener
    """
    start_periodic_task(
        check_all_deleted_topics, interval_minutes=3, task_name="email delete check"
    )
