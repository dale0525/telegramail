from app.i18n import _
from aiotdlib.api import ReplyMarkupShowKeyboard, KeyboardButton, KeyboardButtonTypeText

# Helper function to create yes/no keyboard
def create_yes_no_keyboard() -> ReplyMarkupShowKeyboard:
    """
    Creates a reply keyboard with "yes" and "no" buttons.

    This uses ReplyMarkupShowKeyboard, which presents the keyboard
    to the user as a reply option, rather than an inline keyboard.
    It's suitable for steps in a conversation where a simple text response
    ("yes" or "no") is expected.
    """
    return ReplyMarkupShowKeyboard(
        rows=[
            [
                KeyboardButton(text=_("yes"), type=KeyboardButtonTypeText()),
                KeyboardButton(text=_("no"), type=KeyboardButtonTypeText()),
            ]
        ],
        one_time=True,  # Keyboard hides after one use
        resize_keyboard=True, # Adjust keyboard size
    ) 