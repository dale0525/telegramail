import functools
import inspect
import time
import threading
from typing import Any, Callable, Optional, Tuple, Type, Union


def retry_on_fail(
    max_retries: int = 3,
    retry_delay: float = 1.0,
    exceptions: Union[Type[Exception], Tuple[Type[Exception], ...]] = Exception,
    log_failure: bool = True,
    retry_on_error_message: Optional[str] = None,
):
    """
    Decorator that retries a function if it fails with specified exceptions.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        retry_delay: Delay between retries in seconds (default: 1.0)
        exceptions: Exception type or tuple of exception types to catch and retry (default: Exception)
        log_failure: Whether to log failures (default: True)
        retry_on_error_message: If provided, only retry if the error message contains this string

    Returns:
        The decorator function
    """

    def decorator(func: Callable) -> Callable:
        from app.utils.logger import Logger

        logger = Logger().get_logger(__name__)

        if inspect.iscoroutinefunction(func):
            import asyncio

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                retry_count = 0
                last_exception = None

                while retry_count <= max_retries:
                    try:
                        result = await func(*args, **kwargs)
                        if retry_count > 0:
                            logger.info(
                                f"Successfully executed {func.__name__} after {retry_count} retries"
                            )
                        return result
                    except exceptions as e:
                        last_exception = e

                        # Check if we should retry based on error message
                        if (
                            retry_on_error_message
                            and retry_on_error_message not in str(e)
                        ):
                            raise  # Don't retry if error message doesn't match

                        retry_count += 1

                        if retry_count <= max_retries:
                            if log_failure:
                                logger.warning(
                                    f"Retry {retry_count}/{max_retries} for {func.__name__} due to: {str(e)}"
                                )
                            await asyncio.sleep(retry_delay)
                        else:
                            if log_failure:
                                logger.error(
                                    f"Failed to execute {func.__name__} after {max_retries} retries. Last error: {str(e)}"
                                )
                            raise last_exception

                # This should never be reached, but added for completeness
                raise last_exception

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            retry_count = 0
            last_exception = None

            while retry_count <= max_retries:
                try:
                    result = func(*args, **kwargs)
                    if retry_count > 0:
                        logger.info(
                            f"Successfully executed {func.__name__} after {retry_count} retries"
                        )
                    return result
                except exceptions as e:
                    last_exception = e

                    # Check if we should retry based on error message
                    if retry_on_error_message and retry_on_error_message not in str(e):
                        raise  # Don't retry if error message doesn't match

                    retry_count += 1

                    if retry_count <= max_retries:
                        if log_failure:
                            logger.warning(
                                f"Retry {retry_count}/{max_retries} for {func.__name__} due to: {str(e)}"
                            )
                        time.sleep(retry_delay)
                    else:
                        if log_failure:
                            logger.error(
                                f"Failed to execute {func.__name__} after {max_retries} retries. Last error: {str(e)}"
                            )
                        raise last_exception

            # This should never be reached, but added for completeness
            raise last_exception

        return wrapper

    return decorator


def Singleton(cls):
    """
    Thread-safe singleton decorator.
    Ensures only one instance of a class is created even in concurrent environments.
    """
    _instances = {}
    _lock = threading.Lock()

    @functools.wraps(cls)
    def _singleton(*args, **kwargs):
        if cls not in _instances:
            with _lock:
                # Double-check locking pattern
                if cls not in _instances:
                    _instances[cls] = cls(*args, **kwargs)
        return _instances[cls]

    return _singleton
