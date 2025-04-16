# 日志记录器，单例，使用logging库，默认级别为 .env 中的 LOG_LEVEL 配置

import logging
import os


class Logger:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        self.default_log_level = os.getenv("LOG_LEVEL", "INFO")
        logging.basicConfig(
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            # level=logging.DEBUG,
            level=logging.WARNING,
        )

    def get_logger(self, name):
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, self.default_log_level))
        return logger
