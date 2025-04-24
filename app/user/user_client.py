import os
from aiotdlib import Client, ClientSettings
from aiotdlib.api import (
    CheckAuthenticationCode,
    CheckAuthenticationPassword,
)

from app.utils import Logger
from app.utils.decorators import Singleton

logger = Logger().get_logger(__name__)


class CustomClient(Client):
    async def _check_authentication_code(self):
        from app.bot.handlers.login import login_get_code

        await login_get_code()

    async def send_auth_code(self, code):
        await self.send(
            CheckAuthenticationCode(
                code=code,
            )
        )

    async def _check_authentication_password(self):
        from app.bot.handlers.login import login_get_password

        await login_get_password()

    async def send_password(self, password):
        await self.send(
            CheckAuthenticationPassword(
                password=password,
            )
        )

    async def _auth_completed(self):
        from app.bot.handlers.access import add_phone, get_temp_phone

        phone = get_temp_phone()
        add_phone(phone)

        from app.cron.email_delete_listener import listen_to_email_deletions

        listen_to_email_deletions()
        logger.info("Started email delete listener")

        await super()._auth_completed()


@Singleton
class UserClient:
    def __init__(self):
        self.client = None

    def start(self, phone_number: str | None = None):
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        self.client = CustomClient(
            settings=ClientSettings(
                api_id=api_id,
                api_hash=api_hash,
                phone_number=phone_number,
                database_encryption_key="Telegramail",
                files_directory=os.path.join(os.getcwd(), "data", "user"),
                library_path=os.path.join(
                    os.getcwd(),
                    "app",
                    "resources",
                    "tdlib",
                    "darwin",
                    "libtdjson_darwin_arm64_2.dylib",
                ),
            )
        )

    async def stop(self):
        """
        Stop the user client and perform cleanup tasks
        """
        if self.client:
            # Re-run email deletion listener to catch any last-minute deletions

            from app.cron.email_delete_listener import check_all_deleted_topics

            await check_all_deleted_topics()
            logger.info("checking deleted topics when shutting down")

            # Wait a moment for any pending tasks
            import asyncio

            await asyncio.sleep(1)
