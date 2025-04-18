import os
from aiotdlib.api import UpdateNewMessage

ADMIN_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "admin.txt")
PHONE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "phone.txt")


def validate_admin(update: UpdateNewMessage) -> bool:
    """validate if the user has access to this bot"""
    user_id = update.message.sender_id.user_id
    if not os.path.exists(ADMIN_FILE):
        add_admin(user_id)
        return True
    with open(ADMIN_FILE, "r") as f:
        return str(user_id) == f.read()


def add_admin(user_id: int | str):
    """set the user as bot admin"""
    with open(ADMIN_FILE, "w+") as f:
        f.write(str(user_id))


def get_admin():
    """get the bot admin"""
    if not os.path.exists(ADMIN_FILE):
        return None
    with open(ADMIN_FILE, "r") as f:
        admin_id = f.read()
        if admin_id == "":
            return None
        return int(admin_id)
