"""
TelegramMail - A Telegram bot for receiving and sending emails.
"""
import logging
import os
from typing import Optional
from telegram.ext import Application

__version__ = "0.1.0"

# 获取环境变量中的日志级别
log_level_str = os.getenv("LOG_LEVEL", "INFO")
log_level = getattr(logging, log_level_str)

# 配置日志 - 确保app包的日志器设置为正确的级别
logger = logging.getLogger(__name__)
logger.setLevel(log_level)

# 全局应用实例
_bot_application = None

def set_bot_application(application: Application) -> None:
    """
    设置全局bot应用实例。
    
    Args:
        application: 应用实例
    """
    global _bot_application
    _bot_application = application
    logger.info("全局应用实例已设置")

def get_bot_application() -> Optional[Application]:
    """
    获取全局bot应用实例。
    
    Returns:
        应用实例，如果未设置则返回None
    """
    return _bot_application 