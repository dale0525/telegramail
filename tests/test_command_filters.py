import unittest


class _FakeFormattedText:
    def __init__(self, text: str):
        self.text = text


class _FakeMessageText:
    ID = "messageText"

    def __init__(self, text: str):
        self.text = _FakeFormattedText(text)


class _FakeMessage:
    def __init__(self, text: str):
        self.content = _FakeMessageText(text)


class _FakeUpdate:
    def __init__(self, text: str):
        self.message = _FakeMessage(text)
        self.EXTRA = {}


class TestCommandFilters(unittest.IsolatedAsyncioTestCase):
    def test_parse_bot_command_strips_bot_username(self):
        from app.bot.handlers.command_filters import parse_bot_command

        cmd, mention, args = parse_bot_command("/compose@LogicEmailBot")
        self.assertEqual(cmd, "compose")
        self.assertEqual(mention, "LogicEmailBot")
        self.assertEqual(args, [])

    async def test_make_command_filter_accepts_compose_with_username(self):
        from app.bot.handlers.command_filters import make_command_filter

        update = _FakeUpdate("/compose@LogicEmailBot")
        f = make_command_filter("compose")

        result = await f(update)
        self.assertEqual(result["bot_command"], "compose")
        self.assertEqual(result["bot_command_args"], [])

    async def test_make_command_filter_rejects_other_command(self):
        from app.bot.handlers.command_filters import make_command_filter

        update = _FakeUpdate("/help@LogicEmailBot")
        f = make_command_filter("compose")

        result = await f(update)
        self.assertFalse(result)
