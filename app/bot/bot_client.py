import os
from aiotdlib import Client, ClientSettings

from app.utils.decorators import Singleton
from app.utils.tdlib_manager import get_library_path
from app.utils import Logger

logger = Logger().get_logger(__name__)


@Singleton
class BotClient:
    def __init__(self):
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

        # Get dynamic library path for bot client
        library_path = get_library_path("bot")
        logger.info(f"Using TDLib library for bot: {library_path}")

        self.client = Client(
            settings=ClientSettings(
                api_id=api_id,
                api_hash=api_hash,
                bot_token=bot_token,
                database_encryption_key="Telegramail",
                files_directory=os.path.join(os.getcwd(), "data", "bot"),
                library_path=library_path,
            )
        )
