from aiotdlib import Client
from aiotdlib.api import UpdateNewMessage
import asyncio
from app.bot.handlers.access import validate_admin, get_groups
from app.bot.utils import send_and_delete_message
from app.i18n import _
from app.utils import Logger
from app.email_utils.imap_client import IMAPClient
from app.email_utils.account_manager import AccountManager
from app.bot.conversation import Conversation

logger = Logger().get_logger(__name__)


async def check_command_handler(client: Client, update: UpdateNewMessage):
    """handle /check command"""
    if not validate_admin(update):
        return
    chat_id = update.message.chat_id
    groups = get_groups()
    stop = False
    if groups:
        for email, group_id in groups.items():
            if group_id == chat_id:  # check email for this email account
                logger.debug(f"check email for {email}")
                stop = True
                await check_specific_email(
                    client, chat_id, update.message.sender_id.user_id, email
                )
                break

    # check all emails
    if not stop:
        await check_all_emails(client, chat_id, update.message.sender_id.user_id)


async def check_specific_email(client: Client, chat_id: int, user_id: int, email: str):
    """
    check specific email for new emails using conversation

    Args:
        client: bot client
        chat_id: chat id
        user_id: user id
        email: email to check
    """
    steps = [
        {
            "text": f"📧 {_('checking_emails_for')} <b>{email}</b>...",
            "key": "check_start",
            "action": lambda context: fetch_emails_action(context, email),
            "pre_action_message_key": "fetching_emails_wait",
            "success_message_key": "email_fetch_success",
        }
    ]

    conversation = await Conversation.create_conversation(
        client, chat_id, user_id, steps
    )

    async def on_complete(context):
        email_count = context.get("email_count", 0)
        if email_count > 0:
            conversation.finish_message = (
                f"✅ {_('found_new_emails', count=email_count, email=email)}"
            )
            conversation.finish_message_type = "success"
        else:
            conversation.finish_message = f"ℹ️ {_('no_new_emails_found', email=email)}"
            conversation.finish_message_type = "info"
        conversation.finish_message_delete_after = 3

    conversation.on_finish(on_complete)

    await conversation.start()


async def check_all_emails(client: Client, chat_id: int, user_id: int):
    """
    check all emails for new emails using conversation

    Args:
        client: bot client
        chat_id: chat id
        user_id: user id
    """
    account_manager = AccountManager()
    accounts = account_manager.get_all_accounts()

    if not accounts:
        await send_and_delete_message(client, chat_id, f"❌ {_('no_accounts')}", 3)
        return

    steps = [
        {
            "text": f"📧 {_('checking_all_emails')}...",
            "key": "check_start",
            "action": lambda context: fetch_all_emails_action(context),
            "pre_action_message_key": "fetching_emails_wait",
            "success_message_key": "email_fetch_success",
        }
    ]

    conversation = await Conversation.create_conversation(
        client, chat_id, user_id, steps
    )

    async def on_complete(context):
        email_count = context.get("email_count", 0)
        emails_info = context.get("emails_info", {})

        if email_count > 0:
            details = "\n".join(
                [
                    f"• {email}: {count} {_('new_email_count')}"
                    for email, count in emails_info.items()
                    if count > 0
                ]
            )
            conversation.finish_message = (
                f"✅ {_('found_total_new_emails', count=email_count)}\n\n{details}"
            )
            conversation.finish_message_type = "success"
        else:
            conversation.finish_message = f"ℹ️ {_('no_new_emails_found_all')}"
            conversation.finish_message_type = "info"
        conversation.finish_message_delete_after = 3

    conversation.on_finish(on_complete)

    await conversation.start()


async def fetch_emails_action(context: dict, email: str) -> tuple[bool, str]:
    """
    fetch emails for specific email account

    Args:
        context: conversation context
        email: email address

    Returns:
        (success, message) tuple
    """
    try:
        imap_client = IMAPClient(email)

        email_count = await imap_client.fetch_unread_emails()

        context["email_count"] = email_count

        return True, ""
    except Exception as e:
        logger.error(f"Error fetching emails for {email}: {e}")
        return False, str(e)


async def _fetch_email_for_account(email: str) -> tuple[str, int, str]:
    """
    Fetch emails for a single account
    
    Args:
        email: Email address to fetch from
        
    Returns:
        Tuple of (email address, email count, error message)
        Error message is empty if successful
    """
    try:
        imap_client = IMAPClient(email)
        email_count = await imap_client.fetch_unread_emails()
        return email, email_count, ""
    except Exception as e:
        logger.error(f"Error fetching emails for {email}: {e}")
        return email, 0, str(e)


async def fetch_all_emails_action(context: dict) -> tuple[bool, str]:
    """
    fetch emails for all email accounts concurrently

    Args:
        context: conversation context

    Returns:
        (success, message) tuple
    """
    try:
        account_manager = AccountManager()
        accounts = account_manager.get_all_accounts()

        # Create fetch tasks for all accounts
        fetch_tasks = []
        for account in accounts:
            email = account["email"]
            fetch_tasks.append(_fetch_email_for_account(email))
            
        # Run all fetch tasks concurrently
        results = await asyncio.gather(*fetch_tasks)
        
        # Process results
        total_count = 0
        emails_info = {}
        
        for email, count, error in results:
            if error:
                emails_info[email] = f"Error: {error}"
            else:
                total_count += count
                emails_info[email] = count

        context["email_count"] = total_count
        context["emails_info"] = emails_info

        return True, ""
    except Exception as e:
        logger.error(f"Error fetching all emails: {e}")
        return False, str(e)
