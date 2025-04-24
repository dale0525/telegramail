import datetime
from aiotdlib.api import (
    ChatEventLogFilters,
)
from app.database import DBManager
from app.email_utils.imap_client import IMAPClient
from app.utils import Logger
from app.data.data_manager import DataManager
from app.cron.cron_utils import start_periodic_task

logger = Logger().get_logger(__name__)


async def check_deleted_topics_for_group(chat_id):
    """
    Check deleted topics for a specific group
    """
    try:
        # Get current time and calculate the time threshold (with redundancy)
        now = datetime.datetime.now()
        # Using 5 minutes instead of 3 for redundancy
        time_threshold = now - datetime.timedelta(minutes=5)

        from app.user.user_client import UserClient

        events = await UserClient().client.api.get_chat_event_log(
            chat_id=chat_id,
            query="",
            from_event_id=0,
            limit=100,
            filters=ChatEventLogFilters(forum_changes=True),
            user_ids=[],
        )

        processed_count = 0
        for event in events.events:
            # Skip if event is older than our threshold
            event_time = datetime.datetime.fromtimestamp(event.date)
            if event_time < time_threshold:
                break

            if not event.action.ID == "chatEventForumTopicDeleted":
                continue

            topic_info = event.action.topic_info
            thread_id = topic_info.message_thread_id

            db_manager = DBManager()
            email, email_uid = db_manager.get_email_uid_by_telegram_thread_id(
                str(thread_id)
            )

            if not email or not email_uid:
                continue

            logger.info(f"Deleting email with UID {email_uid} for topic {thread_id}")
            imap_client = IMAPClient(email)
            imap_client.delete_email_by_uid(email_uid)
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
    groups_data = DataManager().get_groups()
    for email, chat_id in groups_data.items():
        logger.info(f"Checking deleted topics for group {chat_id} (email: {email})")
        await check_deleted_topics_for_group(chat_id)


def listen_to_email_deletions():
    """
    Start the email delete listener
    """
    start_periodic_task(
        check_all_deleted_topics, interval_minutes=3, task_name="email delete check"
    )
