import os
import unittest
from unittest import mock


class TestProxyConfig(unittest.TestCase):
    def setUp(self):
        import app.utils.logger as logger_module

        logger_module.Logger.reset_instance()
        self.load_dotenv_patcher = mock.patch("app.utils.logger.load_dotenv")
        self.load_dotenv_patcher.start()
        self.addCleanup(self.load_dotenv_patcher.stop)
        self.addCleanup(logger_module.Logger.reset_instance)

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

    def test_unauthenticated_http_proxy_uses_tdlib_compatible_empty_credentials(self):
        from aiotdlib.api import ProxyTypeHttp

        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings({"http_proxy": "http://127.0.0.1:7890"})

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.username, "")
        self.assertEqual(proxy.password, "")
        tdlib_proxy_type = ProxyTypeHttp(
            username=proxy.username,
            password=proxy.password,
            http_only=proxy.http_only,
        )
        self.assertEqual(tdlib_proxy_type.username, "")
        self.assertEqual(tdlib_proxy_type.password, "")

    def test_unauthenticated_socks5_proxy_uses_tdlib_compatible_empty_credentials(self):
        from aiotdlib.api import ProxyTypeSocks5

        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings({"all_proxy": "socks5://127.0.0.1:1080"})

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.username, "")
        self.assertEqual(proxy.password, "")
        tdlib_proxy_type = ProxyTypeSocks5(
            username=proxy.username,
            password=proxy.password,
        )
        self.assertEqual(tdlib_proxy_type.username, "")
        self.assertEqual(tdlib_proxy_type.password, "")

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

    def test_explicit_app_proxy_overrides_no_proxy_star(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {
                "TELEGRAMAIL_PROXY": "socks5://proxy.example.com:1080",
                "NO_PROXY": "*",
            }
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "proxy.example.com")
        self.assertEqual(proxy.port, 1080)
        self.assertEqual(str(proxy.type), "ClientProxyType.SOCKS5")

    def test_invalid_higher_priority_proxy_falls_back_to_next_valid_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {
                "all_proxy": "ftp://proxy.example.com:21",
                "http_proxy": "http://127.0.0.1:7890",
            }
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "127.0.0.1")
        self.assertEqual(proxy.port, 7890)
        self.assertEqual(str(proxy.type), "ClientProxyType.HTTP")

    def test_https_proxy_url_is_ignored_in_favor_of_next_valid_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {
                "https_proxy": "https://proxy.example.com:443",
                "http_proxy": "http://127.0.0.1:7890",
            }
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "127.0.0.1")
        self.assertEqual(proxy.port, 7890)
        self.assertEqual(str(proxy.type), "ClientProxyType.HTTP")

    def test_zero_proxy_port_is_ignored_in_favor_of_next_valid_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {
                "all_proxy": "socks5://proxy.example.com:0",
                "http_proxy": "http://127.0.0.1:7890",
            }
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "127.0.0.1")
        self.assertEqual(proxy.port, 7890)
        self.assertEqual(str(proxy.type), "ClientProxyType.HTTP")

    def test_no_proxy_star_disables_tdlib_proxy(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {"http_proxy": "http://127.0.0.1:7890", "NO_PROXY": "*"}
        )

        self.assertIsNone(proxy)

    def test_no_proxy_star_disables_aiotdlib_proxy_settings_kwargs(self):
        from app.utils.proxy import build_tdlib_proxy_settings_kwargs

        kwargs = build_tdlib_proxy_settings_kwargs(
            {
                "NO_PROXY": "*",
                "AIOTDLIB_PROXY_SETTINGS": (
                    '{"host":"proxy.example.com","port":1080,"type":"socks5"}'
                ),
            }
        )

        self.assertIn("proxy_settings", kwargs)
        self.assertIsNone(kwargs["proxy_settings"])

    def test_empty_env_does_not_fall_back_to_process_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        with mock.patch.dict(
            os.environ,
            {"http_proxy": "http://127.0.0.1:7890"},
            clear=True,
        ):
            proxy = build_tdlib_proxy_settings({})

        self.assertIsNone(proxy)

    def test_invalid_explicit_app_proxy_disables_fallback_proxy_env(self):
        from app.utils.proxy import build_tdlib_proxy_settings_kwargs

        kwargs = build_tdlib_proxy_settings_kwargs(
            {
                "TELEGRAMAIL_PROXY": "ftp://proxy.example.com:21",
                "http_proxy": "http://127.0.0.1:7890",
            }
        )

        self.assertIn("proxy_settings", kwargs)
        self.assertIsNone(kwargs["proxy_settings"])

    def test_mtproto_proxy_rejects_non_hex_secret(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {"TELEGRAMAIL_PROXY": "mtproto://proxy.example.com:443?secret=not-hex"}
        )

        self.assertIsNone(proxy)

    def test_mtproto_proxy_rejects_secret_with_encoded_spaces(self):
        from app.utils.proxy import build_tdlib_proxy_settings

        proxy = build_tdlib_proxy_settings(
            {"TELEGRAMAIL_PROXY": "mtproto://proxy.example.com:443?secret=aa%20%20bb"}
        )

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

    def test_bot_client_omits_proxy_settings_when_only_aiotdlib_env_exists(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "hash",
                "TELEGRAM_BOT_TOKEN": "bot-token",
                "AIOTDLIB_PROXY_SETTINGS": '{"host":"proxy.example.com","port":1080,"type":"socks5"}',
            },
            clear=True,
        ):
            from app.bot import bot_client

            bot_client.BotClient.reset_instance()
            with (
                mock.patch("app.bot.bot_client.get_library_path", return_value="/tmp/libtdjson"),
                mock.patch("app.bot.bot_client.Client"),
                mock.patch(
                    "app.bot.bot_client.ClientSettings",
                    side_effect=lambda **kwargs: kwargs,
                ) as settings_cls,
            ):
                bot_client.BotClient()

        self.assertNotIn("proxy_settings", settings_cls.call_args.kwargs)

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

    def test_user_client_omits_proxy_settings_when_only_aiotdlib_env_exists(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "hash",
                "AIOTDLIB_PROXY_SETTINGS": '{"host":"proxy.example.com","port":1080,"type":"socks5"}',
            },
            clear=True,
        ):
            from app.user import user_client

            user_client.UserClient.reset_instance()
            with (
                mock.patch("app.user.user_client.get_library_path", return_value="/tmp/libtdjson"),
                mock.patch("app.user.user_client.CustomClient"),
                mock.patch(
                    "app.user.user_client.ClientSettings",
                    side_effect=lambda **kwargs: kwargs,
                ) as settings_cls,
            ):
                user_client.UserClient().start("+123456789")

        self.assertNotIn("proxy_settings", settings_cls.call_args.kwargs)
