import asyncio
import time
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

    async def test_idle_wait_does_not_block_event_loop(self):
        from app.cron.imap_idle_manager import IMAPIdleManager

        _FakeIMAPClient.connect_results = [True]
        tick_elapsed = {"value": None}

        async def _fake_fetch(_account):
            return 0

        async def _fake_sleep(_seconds):
            # This path is not expected in this scenario.
            return None

        manager = IMAPIdleManager(
            imap_client_cls=_FakeIMAPClient,
            supports_idle_fn=lambda _conn: True,
            idle_wait_once_fn=lambda *_args, **_kwargs: _blocking_idle_wait(manager),
            fetch_account_emails_fn=_fake_fetch,
            fallback_poll_seconds=30,
            reconnect_backoff_seconds=5,
            sleep_fn=_fake_sleep,
        )
        manager._running = True

        started_at = time.monotonic()

        async def _tick():
            await asyncio.sleep(0.05)
            tick_elapsed["value"] = time.monotonic() - started_at

        tick_task = asyncio.create_task(_tick())
        watcher_task = asyncio.create_task(
            manager._run_watcher({"id": 1, "email": "a@example.com"}, "INBOX")
        )

        await asyncio.gather(tick_task, watcher_task)

        self.assertIsNotNone(tick_elapsed["value"])
        self.assertLess(
            tick_elapsed["value"],
            0.15,
            f"event loop was blocked by idle wait: {tick_elapsed['value']:.3f}s",
        )


def _blocking_idle_wait(manager):
    time.sleep(0.2)
    manager._running = False
    return False


if __name__ == "__main__":
    unittest.main()
