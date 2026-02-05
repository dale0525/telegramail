from __future__ import annotations

from typing import Any


def parse_bot_command(text: str) -> tuple[str, str | None, list[str]]:
    """
    Parse a Telegram slash-command message.

    Supports both `/cmd` and `/cmd@BotUserName` forms.

    Returns:
        (command, mentioned_bot_username, args)
    """
    if not text:
        return "", None, []

    text = text.strip()
    if not text.startswith("/"):
        return "", None, []

    parts = text.lstrip("/").split()
    if not parts:
        return "", None, []

    token, *args = parts
    if "@" in token:
        command, mention = token.split("@", 1)
        mention = mention or None
    else:
        command, mention = token, None

    return (command or "").strip().lower(), mention, args


def make_command_filter(expected_command: str):
    """
    Build an aiotdlib-compatible filter callable for a specific command.

    Unlike aiotdlib's built-in bot_command filter, this matches both:
    - `/compose`
    - `/compose@YourBot`
    """

    expected = (expected_command or "").strip().lower()

    async def _filter(update: Any):
        try:
            message = getattr(update, "message", None)
            content = getattr(message, "content", None)
            if not content:
                return False

            # aiotdlib: MessageText.ID == "messageText" and content.text is FormattedText
            if getattr(content, "ID", None) != "messageText":
                return False

            formatted = getattr(content, "text", None)
            text = getattr(formatted, "text", None)
            if not isinstance(text, str) or not text:
                return False
        except Exception:
            return False

        cmd, _mention, args = parse_bot_command(text)
        if not cmd or cmd != expected:
            return False

        return {"bot_command": cmd, "bot_command_args": args}

    return _filter

