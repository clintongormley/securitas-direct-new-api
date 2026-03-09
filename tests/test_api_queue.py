"""Tests for ApiQueue — priority-based API rate limiter."""

import asyncio
import time

import pytest

from custom_components.securitas.api_queue import ApiQueue

pytestmark = pytest.mark.asyncio


class TestApiQueueBasic:
    """Basic submit and rate limiting."""

    async def test_submit_executes_coroutine(self):
        queue = ApiQueue(interval=0)

        async def fn():
            return 42

        result = await queue.submit(fn, priority=ApiQueue.BACKGROUND)
        assert result == 42

    async def test_submit_passes_args(self):
        queue = ApiQueue(interval=0)

        async def fn(a, b):
            return a + b

        result = await queue.submit(fn, 3, 7, priority=ApiQueue.BACKGROUND)
        assert result == 10

    async def test_submit_propagates_exception(self):
        queue = ApiQueue(interval=0)

        async def fn():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await queue.submit(fn, priority=ApiQueue.BACKGROUND)

    async def test_last_api_time_updated_on_success(self):
        queue = ApiQueue(interval=0)
        before = time.monotonic()

        async def fn():
            return 1

        await queue.submit(fn, priority=ApiQueue.BACKGROUND)
        assert queue._last_api_time >= before

    async def test_last_api_time_updated_on_error(self):
        queue = ApiQueue(interval=0)

        async def fn():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await queue.submit(fn, priority=ApiQueue.BACKGROUND)
        assert queue._last_api_time > 0


class TestApiQueueRateLimiting:
    """Minimum gap enforcement."""

    async def test_background_enforces_interval(self):
        queue = ApiQueue(interval=0.1)
        times = []

        async def fn():
            times.append(time.monotonic())

        await queue.submit(fn, priority=ApiQueue.BACKGROUND)
        await queue.submit(fn, priority=ApiQueue.BACKGROUND)
        assert times[1] - times[0] >= 0.09  # allow small float error


class TestApiQueuePriority:
    """Foreground preemption of background work."""

    async def test_foreground_runs_before_queued_background(self):
        """When foreground and background are both waiting, foreground goes first."""
        queue = ApiQueue(interval=0.05)
        order = []

        # Hold the lock with an initial call
        release = asyncio.Event()

        async def blocker():
            await release.wait()
            return "blocker"

        async def bg():
            order.append("bg")

        async def fg():
            order.append("fg")

        # Start blocker (holds the lock)
        blocker_task = asyncio.create_task(
            queue.submit(blocker, priority=ApiQueue.BACKGROUND)
        )
        await asyncio.sleep(0.01)  # let blocker acquire lock

        # Queue background then foreground
        bg_task = asyncio.create_task(queue.submit(bg, priority=ApiQueue.BACKGROUND))
        await asyncio.sleep(0.01)
        fg_task = asyncio.create_task(queue.submit(fg, priority=ApiQueue.FOREGROUND))
        await asyncio.sleep(0.01)

        # Release blocker
        release.set()
        await asyncio.gather(blocker_task, bg_task, fg_task)

        assert order[0] == "fg"
        assert order[1] == "bg"

    async def test_background_yields_while_foreground_pending(self):
        """Background waits while foreground work is pending."""
        queue = ApiQueue(interval=0)
        events = []

        # Simulate: foreground is "pending" (incremented but not yet submitted)
        queue._pending_foreground = 1
        queue._bg_event.clear()

        async def bg():
            events.append("bg")

        # Background should block
        bg_task = asyncio.create_task(queue.submit(bg, priority=ApiQueue.BACKGROUND))
        await asyncio.sleep(0.05)
        assert "bg" not in events  # still waiting

        # Clear foreground
        queue._pending_foreground = 0
        queue._bg_event.set()
        await bg_task
        assert "bg" in events
