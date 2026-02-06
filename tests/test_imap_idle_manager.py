import unittest


class _FakeConn:
    def select(self, _mailbox):
        return "OK", []


class _FakeIMAPClient:
    connect_results = []

    def __init__(self, account):
        self.account = account
        self.conn = _FakeConn()

    def connect(self):
        if self.connect_results:
            return self.connect_results.pop(0)
        return True

    def disconnect(self):
        return None

    def _get_monitored_mailboxes(self):
        return ["INBOX"]


class TestImapIdleManager(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_short_poll_when_idle_unsupported(self):
        from app.cron.imap_idle_manager import IMAPIdleManager

        _FakeIMAPClient.connect_results = [True]
        fetch_calls = []
        sleep_calls = []

        async def _fake_fetch(account):
            fetch_calls.append(account["email"])

        async def _fake_sleep(seconds):
            sleep_calls.append(seconds)
            manager._running = False

        manager = IMAPIdleManager(
            imap_client_cls=_FakeIMAPClient,
            supports_idle_fn=lambda _conn: False,
            idle_wait_once_fn=lambda *_args, **_kwargs: False,
            fetch_account_emails_fn=_fake_fetch,
            fallback_poll_seconds=30,
            reconnect_backoff_seconds=5,
            sleep_fn=_fake_sleep,
        )
        manager._running = True

        await manager._run_watcher({"id": 1, "email": "a@example.com"}, "INBOX")

        self.assertEqual(fetch_calls, ["a@example.com"])
        self.assertEqual(sleep_calls, [30])

    async def test_reconnect_backoff_grows_exponentially_before_success(self):
        from app.cron.imap_idle_manager import IMAPIdleManager

        _FakeIMAPClient.connect_results = [False, False, True]
        fetch_calls = []
        sleep_calls = []

        async def _fake_fetch(account):
            fetch_calls.append(account["email"])

        async def _fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                manager._running = False

        manager = IMAPIdleManager(
            imap_client_cls=_FakeIMAPClient,
            supports_idle_fn=lambda _conn: False,
            idle_wait_once_fn=lambda *_args, **_kwargs: False,
            fetch_account_emails_fn=_fake_fetch,
            fallback_poll_seconds=30,
            reconnect_backoff_seconds=5,
            sleep_fn=_fake_sleep,
        )
        manager._running = True

        await manager._run_watcher({"id": 1, "email": "a@example.com"}, "INBOX")

        self.assertEqual(sleep_calls[:3], [5, 10, 30])
        self.assertEqual(fetch_calls, ["a@example.com"])


if __name__ == "__main__":
    unittest.main()
