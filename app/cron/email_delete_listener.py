import datetime
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
        # Get current time and calculate the time threshold (with redundancy)
        now = datetime.datetime.now()
        # Using 5 minutes instead of 3 for redundancy
        time_threshold = now - datetime.timedelta(minutes=10)

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
            # Assume get_email_uid_by_telegram_thread_id now returns (account_id, list_of_uids)
            # or None if not found.
            result = db_manager.get_email_uid_by_telegram_thread_id(str(thread_id))

            if result:
                account_id, email_uids = result
                if (
                    account_id and email_uids
                ):  # Check if account_id and the list are valid
                    logger.info(
                        f"Found {len(email_uids)} emails associated with topic {thread_id} for account {account_id}"
                    )
                    account_manager = AccountManager()
                    account = account_manager.get_account(id=account_id)
                    if account:  # Ensure account exists
                        imap_client = IMAPClient(account)
                        deleted_count_in_loop = 0
                        for email_uid in email_uids:
                            try:
                                logger.info(
                                    f"Attempting to delete email with UID {email_uid} for topic {thread_id}"
                                )
                                imap_client.delete_email_by_uid(email_uid)
                                deleted_count_in_loop += 1
                            except Exception as delete_error:
                                logger.error(
                                    f"Error deleting email UID {email_uid} for topic {thread_id}: {delete_error}"
                                )
                        if deleted_count_in_loop > 0:
                            logger.info(
                                f"Successfully deleted {deleted_count_in_loop} emails for topic {thread_id}"
                            )
                        # Increment processed_count once per event if we found associated emails and attempted deletion.
                        processed_count += 1
                    else:
                        logger.warning(
                            f"Account with ID {account_id} not found for topic {thread_id}"
                        )
                else:
                    # Log if result is returned but account_id or email_uids list is empty/invalid
                    logger.info(
                        f"No valid account_id or email UIDs found for topic {thread_id}, though an association record might exist."
                    )
            else:
                # Log if no association was found at all by the db manager function
                logger.info(f"No email association found for topic {thread_id}")

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
