"""Regression coverage for worker supervision and readiness semantics."""

from __future__ import annotations

import asyncio
import time
import unittest

import httpx
from fastapi import FastAPI

from app.main import _install_worker_readiness_route
from app.workers.runtime import MailWorkerRuntime


class _Counter:
    def __init__(self) -> None:
        self.calls = 0
        self.called = asyncio.Event()

    async def run_once(self) -> None:
        self.calls += 1
        self.called.set()


class _Failure:
    async def run_once(self) -> None:
        raise RuntimeError("provider failure must not stop sibling queues")


class _BatchProjection:
    def __init__(self) -> None:
        self.queued = 0
        self.projected = 0
        self.drained = asyncio.Event()

    async def run_once(self, *, include_failed: bool = True):
        if self.queued == 0:
            return None
        self.queued -= 1
        self.projected += 1
        if self.queued == 0:
            self.drained.set()
        return object()


class _BatchIngestion:
    def __init__(self, projection: _BatchProjection) -> None:
        self.projection = projection
        self.ingested = False

    async def ingest_account(self, account) -> int:
        if self.ingested:
            return 0
        self.ingested = True
        self.projection.queued += 3
        return 3


class _BatchDeletion:
    def __init__(self) -> None:
        self.queued = 4
        self.drained = asyncio.Event()

    async def run_once(self):
        if self.queued == 0:
            return None
        self.queued -= 1
        if self.queued == 0:
            self.drained.set()
        return type("DeleteResult", (), {"id": str(self.queued), "state": "tombstoned"})()


class _BatchSummary:
    def __init__(self) -> None:
        self.queued = 3
        self.calls = 0

    async def run_once(self):
        self.calls += 1
        if self.queued == 0:
            return None
        self.queued -= 1
        return object()


class RuntimeHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_interrupts_the_polling_delay(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=30)
        outbox = _Counter()
        runtime.outbox = outbox
        await runtime.start()
        try:
            await asyncio.wait_for(outbox.called.wait(), timeout=0.3)
            first_calls = outbox.calls
            outbox.called.clear()
            runtime.wake()
            await asyncio.wait_for(outbox.called.wait(), timeout=0.3)
            self.assertGreater(outbox.calls, first_calls)
        finally:
            await runtime.stop()

    async def test_newly_ingested_batch_is_projected_before_runtime_sleeps(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=30)
        projection = _BatchProjection()
        runtime.projection = projection
        runtime.ingestion = _BatchIngestion(projection)
        runtime.accounts = lambda: [object()]

        await runtime.start()
        try:
            await asyncio.wait_for(projection.drained.wait(), timeout=0.3)
            self.assertEqual(projection.projected, 3)
        finally:
            await runtime.stop()

    async def test_fresh_projection_backlog_yields_after_one_item_and_wakes_next_cycle(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=30)
        projection = _BatchProjection()
        projection.queued = 3
        runtime.projection = projection

        succeeded = await runtime._drain_fresh_projections()

        self.assertTrue(succeeded)
        self.assertEqual(projection.projected, 1)
        self.assertEqual(projection.queued, 2)
        self.assertTrue(runtime._wake_requested.is_set())

    async def test_woken_delete_batch_is_drained_before_runtime_sleeps(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=30)
        deletion = _BatchDeletion()
        runtime.deletion = deletion

        await runtime.start()
        try:
            await asyncio.wait_for(deletion.drained.wait(), timeout=0.3)
            self.assertEqual(deletion.queued, 0)
        finally:
            await runtime.stop()

    async def test_summary_backlog_yields_after_one_item_and_wakes_next_cycle(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=30)
        summary = _BatchSummary()
        runtime.summary = summary

        succeeded = await runtime._drain_summaries()

        self.assertTrue(succeeded)
        self.assertEqual(summary.calls, 1)
        self.assertEqual(summary.queued, 2)
        self.assertTrue(runtime._wake_requested.is_set())

    async def test_queue_failure_is_isolated_from_sibling_queue(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=0.01)
        runtime.outbox = _Failure()
        deletion = _Counter()
        runtime.deletion = deletion
        await runtime.start()
        try:
            await asyncio.wait_for(deletion.called.wait(), timeout=0.3)
            self.assertGreaterEqual(deletion.calls, 1)
            self.assertIsNotNone(runtime._task)
            self.assertFalse(runtime._task.done())
            self.assertEqual(runtime.readiness()[1]["last_error_component"], "outbox")
        finally:
            await runtime.stop()

    async def test_recent_successful_cycle_is_ready(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=0.01)
        outbox = _Counter()
        runtime.outbox = outbox
        await runtime.start()
        try:
            await asyncio.wait_for(outbox.called.wait(), timeout=0.3)
            ready, payload = runtime.readiness()
            self.assertTrue(ready)
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["worker_task_alive"])
        finally:
            await runtime.stop()

    async def test_unexpected_supervisor_exit_is_restarted(self) -> None:
        runtime = MailWorkerRuntime(poll_seconds=0.1)
        runtime.outbox = _Counter()
        await runtime.start()
        original = runtime._task
        assert original is not None
        original.cancel()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if runtime._task is not original and runtime._task is not None:
                break
        try:
            self.assertIsNot(runtime._task, original)
            self.assertGreaterEqual(runtime.readiness()[1]["restart_count"], 1)
        finally:
            await runtime.stop()

    async def test_readiness_route_rejects_stale_or_absent_worker(self) -> None:
        app = FastAPI()

        @app.get("/health/ready")
        async def old_ready():
            return {"status": "old"}

        @app.get("/{path:path}")
        async def spa(path: str):
            return {"path": path}

        _install_worker_readiness_route(app)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            missing = await client.get("/health/ready")
            self.assertEqual(missing.status_code, 503)

            runtime = MailWorkerRuntime(poll_seconds=0.1)
            runtime.outbox = _Counter()
            await runtime.start()
            await asyncio.wait_for(runtime.outbox.called.wait(), timeout=0.3)
            app.state.worker_runtime = runtime
            healthy = await client.get("/health/ready")
            self.assertEqual(healthy.status_code, 200)

            runtime._last_cycle_completed_at = time.monotonic() - 31
            stale = await client.get("/health/ready")
            self.assertEqual(stale.status_code, 503)
            await runtime.stop()
        finally:
            await client.aclose()
