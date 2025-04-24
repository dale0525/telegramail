import asyncio
import re
from aiotdlib import Client

from app.bot.bot_client import BotClient
from app.bot.conversation import Conversation
from app.bot.handlers.access import get_admin
from app.i18n import _


async def login_get_phone(
    client: Client,
    chat_id: int | None = None,
    user_id: int | None = None,
):
    """ask admin for telegram phone number"""

    steps = [
        {
            "text": _("login_input_phone"),
            "key": "phone",
            "validate": lambda x: (
                bool(re.match(r"^[\d\+]+$", x)),
                _("login_invalid_phone"),
            ),
        },
    ]

    conversation = await Conversation.create_conversation(
        client, chat_id, user_id, steps
    )

    # when conversation ends
    async def on_complete(context):
        from app.main import run_user
        from app.bot.handlers.access import add_temp_phone

        phone_number = context.get("phone")
        add_temp_phone(phone_number)
        asyncio.create_task(run_user(phone_number=phone_number))
        conversation.finish_message = _("login_authing")
        conversation.finish_message_type = "success"
        conversation.finish_message_delete_after = 3

    conversation.on_finish(on_complete)

    # Start the conversation
    await conversation.start()


async def login_get_code():
    """ask admin for telegram auth ocode"""

    steps = [
        {
            "text": _("login_input_auth_code"),
            "key": "auth_code",
            "validate": lambda x: (
                bool(re.match(r"\d{5}$", x)),
                _("login_invalid_auth_code"),
            ),
        },
    ]

    chat_id, user_id = get_admin()

    conversation = await Conversation.create_conversation(
        BotClient().client, chat_id, user_id, steps
    )

    # when conversation ends
    async def on_complete(context):
        from app.user.user_client import UserClient

        auth_code = context.get("auth_code")
        # reverse auth code to avoid incomplete login attempt from Telegram
        auth_code = auth_code[::-1]
        asyncio.create_task(UserClient().client.send_auth_code(auth_code))
        conversation.finish_message = _("login_auth_code_received")
        conversation.finish_message_type = "success"
        conversation.finish_message_delete_after = 3

    conversation.on_finish(on_complete)

    # Start the conversation
    await conversation.start()


async def login_get_password():
    """ask admin for telegram password"""

    steps = [
        {
            "text": _("login_input_password"),
            "key": "password",
        },
    ]

    chat_id, user_id = get_admin()

    conversation = await Conversation.create_conversation(
        BotClient().client, chat_id, user_id, steps
    )

    # when conversation ends
    async def on_complete(context):
        from app.user.user_client import UserClient

        password = context.get("password")
        asyncio.create_task(UserClient().client.send_password(password))
        conversation.finish_message = _("login_password_received")
        conversation.finish_message_type = "success"
        conversation.finish_message_delete_after = 3

    conversation.on_finish(on_complete)

    # Start the conversation
    await conversation.start()
