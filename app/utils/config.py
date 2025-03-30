"""
Configuration module for TelegramMail.
"""
import os
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """
    Configuration class for TelegramMail.
    """

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    OWNER_CHAT_ID: str = os.getenv("OWNER_CHAT_ID", "")

    # Application settings
    POLLING_INTERVAL: int = int(os.getenv("POLLING_INTERVAL", "300"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///telegramail.db")

    @classmethod
    def validate(cls) -> bool:
        """
        Validate the configuration.
        
        Returns:
            bool: True if the configuration is valid, False otherwise.
        """
        # Check required settings
        if not cls.TELEGRAM_BOT_TOKEN:
            print("Error: TELEGRAM_BOT_TOKEN is not set")
            return False

        if not cls.OWNER_CHAT_ID:
            print("Error: OWNER_CHAT_ID is not set")
            return False

        return True


# Create a singleton config instance
config = Config() 