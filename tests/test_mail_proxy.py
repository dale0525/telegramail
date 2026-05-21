import os
import unittest
from unittest import mock


class TestMailProxyConfig(unittest.TestCase):
    def test_builds_mail_http_proxy_from_https_proxy_env(self):
        from app.utils.mail_proxy import build_mail_proxy_config

        proxy = build_mail_proxy_config(
            target_host="imap.gmail.com",
            env={"https_proxy": "http://user:p%40ss@proxy.local:8080"},
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.scheme, "http")
        self.assertEqual(proxy.host, "proxy.local")
        self.assertEqual(proxy.port, 8080)
        self.assertEqual(proxy.username, "user")
        self.assertEqual(proxy.password, "p@ss")

    def test_http_proxy_tunnel_sends_connect_request(self):
        from app.utils.mail_proxy import MailProxyConfig, create_proxied_socket

        fake_socket = _FakeSocket(
            [
                b"HTTP/1.1 200 Connection Established\r\n",
                b"Proxy-Agent: test\r\n\r\n",
            ]
        )

        with mock.patch(
            "app.utils.mail_proxy.socket.create_connection",
            return_value=fake_socket,
        ) as create_connection:
            sock = create_proxied_socket(
                "imap.gmail.com",
                993,
                MailProxyConfig(
                    scheme="http",
                    host="proxy.local",
                    port=8080,
                    username="user",
                    password="p@ss",
                ),
                timeout=15,
            )

        self.assertIs(sock, fake_socket)
        create_connection.assert_called_once_with(("proxy.local", 8080), timeout=15)
        request = b"".join(fake_socket.sent).decode("ascii")
        self.assertIn("CONNECT imap.gmail.com:993 HTTP/1.1\r\n", request)
        self.assertIn("Host: imap.gmail.com:993\r\n", request)
        self.assertIn("Proxy-Authorization: Basic dXNlcjpwQHNz\r\n", request)


class TestConnectionFactoryMailProxy(unittest.TestCase):
    def test_create_imap_ssl_connection_uses_mail_proxy_env(self):
        from app.email_utils.connection_factory import ConnectionFactory

        with mock.patch.dict(
            os.environ,
            {"https_proxy": "http://proxy.local:8080"},
            clear=True,
        ):
            with (
                mock.patch(
                    "app.email_utils.connection_factory.ProxiedIMAP4_SSL",
                    create=True,
                ) as proxied_imap,
                mock.patch(
                    "app.email_utils.connection_factory.imaplib.IMAP4_SSL"
                ) as direct_imap,
            ):
                ConnectionFactory.create_imap_connection(
                    "imap.gmail.com",
                    993,
                    True,
                    timeout=15,
                )

        proxied_imap.assert_called_once()
        direct_imap.assert_not_called()
        self.assertEqual(proxied_imap.call_args.kwargs["host"], "imap.gmail.com")
        self.assertEqual(proxied_imap.call_args.kwargs["port"], 993)
        self.assertEqual(proxied_imap.call_args.kwargs["timeout"], 15)
        self.assertEqual(
            proxied_imap.call_args.kwargs["proxy_config"].host,
            "proxy.local",
        )

    def test_create_smtp_ssl_connection_uses_mail_proxy_env(self):
        from app.email_utils.connection_factory import ConnectionFactory

        with mock.patch.dict(
            os.environ,
            {"https_proxy": "http://proxy.local:8080"},
            clear=True,
        ):
            with (
                mock.patch(
                    "app.email_utils.connection_factory.ProxiedSMTP_SSL",
                    create=True,
                ) as proxied_smtp,
                mock.patch(
                    "app.email_utils.connection_factory.smtplib.SMTP_SSL"
                ) as direct_smtp,
            ):
                ConnectionFactory.create_smtp_connection(
                    "smtp.gmail.com",
                    465,
                    True,
                    timeout=15,
                )

        proxied_smtp.assert_called_once()
        direct_smtp.assert_not_called()
        self.assertEqual(proxied_smtp.call_args.kwargs["host"], "smtp.gmail.com")
        self.assertEqual(proxied_smtp.call_args.kwargs["port"], 465)
        self.assertEqual(proxied_smtp.call_args.kwargs["timeout"], 15)
        self.assertEqual(
            proxied_smtp.call_args.kwargs["proxy_config"].host,
            "proxy.local",
        )


class _FakeSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
