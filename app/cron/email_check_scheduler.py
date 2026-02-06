from app.bot.handlers.check_email import fetch_all_emails_action
from app.cron.cron_utils import start_periodic_task
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def auto_check_emails():
    """
    Automatically check all email accounts for new emails
    This function is called periodically by the scheduler
    """
    try:
        logger.info("Starting automatic email check")
        context = {}
        success, error_message = await fetch_all_emails_action(context)
        
        if success:
            email_count = context.get("email_count", 0)
            if email_count > 0:
                logger.info(f"Auto check found {email_count} new emails")
            else:
                logger.info("Auto check found no new emails")
        else:
            logger.error(f"Auto check failed: {error_message}")
    except Exception as e:
        logger.error(f"Error in automatic email check: {e}")


def start_email_check_scheduler(interval_seconds=300):
    """
    Start the periodic email check scheduler
    
    Args:
        interval_seconds: Interval between email checks in seconds (default: 300)
    """
    interval_minutes = max(interval_seconds, 1) / 60
    task = start_periodic_task(
        auto_check_emails, 
        interval_minutes=interval_minutes, 
        task_name="automatic email check"
    )
    logger.info(f"Email check scheduler started with {interval_seconds} second interval")
    return task
