import os
import unittest
from unittest import mock


class TestProxyConfig(unittest.TestCase):
    def test_builds_tdlib_http_proxy_from_lowercase_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {"http_proxy": "http://user:p%40ss@127.0.0.1:7890"}
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "127.0.0.1")
        self.assertEqual(proxy.port, 7890)
        self.assertEqual(str(proxy.type), "ClientProxyType.HTTP")
        self.assertEqual(proxy.username, "user")
        self.assertEqual(proxy.password, "p@ss")
        self.assertFalse(proxy.http_only)

    def test_builds_tdlib_socks5_proxy_from_explicit_app_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {
                "TELEGRAMAIL_PROXY": "socks5://proxy.example.com:1080",
                "http_proxy": "http://127.0.0.1:7890",
            }
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "proxy.example.com")
        self.assertEqual(proxy.port, 1080)
        self.assertEqual(str(proxy.type), "ClientProxyType.SOCKS5")

    def test_no_proxy_star_disables_tdlib_proxy(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {"http_proxy": "http://127.0.0.1:7890", "NO_PROXY": "*"}
        )

        self.assertIsNone(proxy)

    def test_empty_env_does_not_fall_back_to_process_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        with mock.patch.dict(
            os.environ,
            {"http_proxy": "http://127.0.0.1:7890"},
            clear=True,
        ):
            proxy = build_tdlib_proxy_settings({})

        self.assertIsNone(proxy)

    def test_bot_client_passes_env_proxy_to_tdlib_settings(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "hash",
                "TELEGRAM_BOT_TOKEN": "bot-token",
                "http_proxy": "http://127.0.0.1:7890",
            },
            clear=True,
        ):
            from app.bot import bot_client

            bot_client.BotClient.reset_instance()
            with (
                mock.patch("app.bot.bot_client.get_library_path", return_value="/tmp/libtdjson"),
                mock.patch("app.bot.bot_client.Client") as client_cls,
            ):
                bot_client.BotClient()

        settings = client_cls.call_args.kwargs["settings"]
        self.assertIsNotNone(settings.proxy_settings)
        self.assertEqual(settings.proxy_settings.host, "127.0.0.1")
        self.assertEqual(settings.proxy_settings.port, 7890)

    def test_user_client_passes_env_proxy_to_tdlib_settings(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "hash",
                "http_proxy": "http://127.0.0.1:7890",
            },
            clear=True,
        ):
            from app.user import user_client

            user_client.UserClient.reset_instance()
            with (
                mock.patch("app.user.user_client.get_library_path", return_value="/tmp/libtdjson"),
                mock.patch("app.user.user_client.CustomClient") as client_cls,
            ):
                user_client.UserClient().start("+123456789")

        settings = client_cls.call_args.kwargs["settings"]
        self.assertIsNotNone(settings.proxy_settings)
        self.assertEqual(settings.proxy_settings.host, "127.0.0.1")
        self.assertEqual(settings.proxy_settings.port, 7890)
