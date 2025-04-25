from app.i18n import _
from aiotdlib.api import ReplyMarkupShowKeyboard, KeyboardButton, KeyboardButtonTypeText
from app.email_utils.common_providers import COMMON_PROVIDERS


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
        resize_keyboard=True,  # Adjust keyboard size
    )


def create_providers_keyboard() -> ReplyMarkupShowKeyboard:
    """
    Creates a reply keyboard with common email providers and a custom option.

    Presents a grid of buttons with provider names, allowing users to select
    a pre-configured email provider or choose to manually input settings.
    """
    # Put providers in rows of 2 buttons each
    rows = []
    current_row = []

    # Add each provider as a button
    for provider in COMMON_PROVIDERS:
        # Create a button with the provider name
        button = KeyboardButton(text=provider["name"], type=KeyboardButtonTypeText())

        if len(current_row) < 2:
            current_row.append(button)
        else:
            rows.append(current_row)
            current_row = [button]

    # Add any remaining buttons in the last row
    if current_row:
        rows.append(current_row)

    # Add "Custom" as the last option in its own row
    rows.append(
        [
            KeyboardButton(
                text=_("add_addcount_provider_custom"), type=KeyboardButtonTypeText()
            )
        ]
    )

    return ReplyMarkupShowKeyboard(
        rows=rows,
        one_time=True,  # Keyboard hides after one use
        resize_keyboard=True,  # Adjust keyboard size
    )
