import asyncio
import unittest


class _FakeIdleManager:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1

    async def stop(self):
        self.stop_calls += 1


class TestEmailReceiveRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_polling_mode_starts_only_polling(self):
        from app.cron.email_receive_runtime import EmailReceiveRuntime

        idle_manager = _FakeIdleManager()
        started_intervals = []

        async def _sleep_forever():
            await asyncio.sleep(3600)

        def _start_polling(interval_seconds):
            started_intervals.append(interval_seconds)
            return asyncio.create_task(_sleep_forever())

        runtime = EmailReceiveRuntime(
            mode="polling",
            polling_interval_seconds=123,
            idle_manager=idle_manager,
            polling_starter=_start_polling,
        )
        runtime.start()

        self.assertEqual(started_intervals, [123])
        self.assertEqual(idle_manager.start_calls, 0)

        await runtime.stop()
        self.assertEqual(idle_manager.stop_calls, 0)

    async def test_idle_mode_starts_only_idle_manager(self):
        from app.cron.email_receive_runtime import EmailReceiveRuntime

        idle_manager = _FakeIdleManager()
        started_intervals = []

        def _start_polling(interval_seconds):
            started_intervals.append(interval_seconds)
            return None

        runtime = EmailReceiveRuntime(
            mode="idle",
            polling_interval_seconds=123,
            idle_manager=idle_manager,
            polling_starter=_start_polling,
        )
        runtime.start()

        self.assertEqual(started_intervals, [])
        self.assertEqual(idle_manager.start_calls, 1)

        await runtime.stop()
        self.assertEqual(idle_manager.stop_calls, 1)

    async def test_hybrid_mode_starts_both(self):
        from app.cron.email_receive_runtime import EmailReceiveRuntime

        idle_manager = _FakeIdleManager()
        started_intervals = []

        async def _sleep_forever():
            await asyncio.sleep(3600)

        def _start_polling(interval_seconds):
            started_intervals.append(interval_seconds)
            return asyncio.create_task(_sleep_forever())

        runtime = EmailReceiveRuntime(
            mode="hybrid",
            polling_interval_seconds=45,
            idle_manager=idle_manager,
            polling_starter=_start_polling,
        )
        runtime.start()

        self.assertEqual(started_intervals, [45])
        self.assertEqual(idle_manager.start_calls, 1)

        await runtime.stop()
        self.assertEqual(idle_manager.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
