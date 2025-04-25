import json
import os
from aiotdlib.api import UpdateNewMessage

ADMIN_FILE = os.path.join(os.getcwd(), "data", "admin.txt")
TEMP_PHONE_FILE = os.path.join(os.getcwd(), "data", "temp_phone.txt")


def validate_admin(update: UpdateNewMessage) -> bool:
    """validate if the user has access to this bot"""
    user_id = update.message.sender_id.user_id
    if not os.path.exists(ADMIN_FILE):
        add_admin(user_id, update.message.chat_id)
        return True
    with open(ADMIN_FILE, "r") as f:
        admin_data = f.read().strip()
        if not admin_data:
            return False
        admin_id = admin_data.split(":", 1)[0]
        return str(user_id) == admin_id


def add_admin(user_id: int | str, chat_id: int | str = None, phone: str = None):
    """set the user as bot admin

    Args:
        user_id: The user ID of the admin
        chat_id: The chat ID of the admin (optional)
        phone: The phone number of the admin (optional)
    """
    admin_data = f"{user_id}"
    if chat_id is not None:
        admin_data += f":{chat_id}"
    else:
        admin_data += ":"

    if phone is not None:
        admin_data += f":{phone}"

    with open(ADMIN_FILE, "w+") as f:
        f.write(admin_data)


def get_admin_data():
    """Get raw admin data

    Returns:
        tuple: (admin_user_id, admin_chat_id, phone) or (None, None, None) if no admin is set
    """
    if not os.path.exists(ADMIN_FILE):
        return None, None, None
    with open(ADMIN_FILE, "r") as f:
        admin_data = f.read().strip()
        if admin_data == "":
            return None, None, None

        parts = admin_data.split(":", 2)
        admin_id = int(parts[0]) if parts[0] else None
        chat_id = int(parts[1]) if len(parts) > 1 and parts[1] else None
        phone = parts[2] if len(parts) > 2 else None

        return admin_id, chat_id, phone


def get_admin():
    """get the bot admin

    Returns:
        tuple: (admin_user_id, admin_chat_id) or (None, None) if no admin is set
    """
    admin_id, chat_id, _ = get_admin_data()
    return admin_id, chat_id


def add_phone(phone: str):
    """add phone number to admin file"""
    admin_id, chat_id, _ = get_admin_data()
    add_admin(admin_id, chat_id, phone)


def get_phone():
    """get the admin phone number"""
    _, _, phone = get_admin_data()
    return phone


def add_temp_phone(phone: str):
    with open(TEMP_PHONE_FILE, "w+") as f:
        f.write(phone)


def get_temp_phone() -> str | None:
    if not os.path.exists(TEMP_PHONE_FILE):
        return None
    with open(TEMP_PHONE_FILE, "r") as f:
        phone = f.read()
        if phone == "":
            return None
        return phone
