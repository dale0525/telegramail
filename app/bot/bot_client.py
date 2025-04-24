import os
from aiotdlib import Client, ClientSettings

from app.utils.decorators import Singleton


@Singleton
class BotClient:
    def __init__(self):
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.client = Client(
            settings=ClientSettings(
                api_id=api_id,
                api_hash=api_hash,
                bot_token=bot_token,
                database_encryption_key="Telegramail",
                files_directory=os.path.join(os.getcwd(), "data", "bot"),
                library_path=os.path.join(
                    os.getcwd(),
                    "app",
                    "resources",
                    "tdlib",
                    "darwin",
                    "libtdjson_darwin_arm64_1.dylib",
                ),
            )
        )
