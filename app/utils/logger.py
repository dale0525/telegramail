import logging
import os
from app.utils.decorators import Singleton
from dotenv import load_dotenv


class LoggerWrapper:
    """
    Wrapper for standard logging.Logger that sets exc_info=True by default for error method.
    """

    def __init__(self, logger):
        self.logger = logger

    def error(self, msg, *args, exc_info=True, **kwargs):
        """
        Log an error message with exception info by default
        """
        return self.logger.error(msg, *args, exc_info=exc_info, **kwargs)

    def __getattr__(self, name):
        """
        Delegate all other methods to the wrapped logger
        """
        return getattr(self.logger, name)


@Singleton
class Logger:
    def __init__(self):
        load_dotenv()
        self.default_log_level = os.getenv("LOG_LEVEL", "INFO")
        print(self.default_log_level)
        # Set root logger level to WARNING by default
        # This affects third-party libraries unless they configure their own loggers
        logging.basicConfig(
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=logging.WARNING,  # Default level for all loggers initially
        )

    def get_logger(self, name):
        """
        Get a logger instance with the level set by LOG_LEVEL environment variable.
        """
        logger = logging.getLogger(name)
        # Set the level specifically for this logger, overriding the root logger's default
        logger.setLevel(getattr(logging, self.default_log_level))
        return LoggerWrapper(logger)
