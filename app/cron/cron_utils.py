import asyncio
from app.utils import Logger

logger = Logger().get_logger(__name__)

async def run_periodic_task(task_func, interval_minutes=3, task_name="periodic task"):
    """
    Run a periodic task at specified intervals
    
    Args:
        task_func: Async function to run periodically
        interval_minutes: Interval between task executions in minutes
        task_name: Name of the task for logging purposes
    """
    while True:
        try:
            logger.info(f"Starting {task_name}")
            await task_func()
        except Exception as e:
            logger.error(f"Error in {task_name}: {e}")
        finally:
            # Wait for the next execution
            await asyncio.sleep(interval_minutes * 60)


def start_periodic_task(task_func, interval_minutes=3, task_name="periodic task"):
    """
    Start a periodic task in the background
    
    Args:
        task_func: Async function to run periodically
        interval_minutes: Interval between task executions in minutes
        task_name: Name of the task for logging purposes
    """
    logger.info(f"Starting {task_name} scheduler")
    loop = asyncio.get_event_loop()
    loop.create_task(run_periodic_task(task_func, interval_minutes, task_name))
