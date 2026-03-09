"""Priority-based API rate limiter for Securitas Direct.

All API calls go through an ApiQueue which enforces minimum gaps between
requests and lets foreground (user-initiated) requests preempt background
(periodic polling) work.
"""

import asyncio
import logging
import time

_LOGGER = logging.getLogger(__name__)

# Default intervals
DEFAULT_FOREGROUND_INTERVAL: float = 2.0
DEFAULT_BACKGROUND_INTERVAL: float = 2.0
DEFAULT_WAF_COOLDOWN: float = 60.0


class ApiQueue:
    """Serialize API calls with priority-based rate limiting.

    Two priority levels:
    - FOREGROUND (min gap = foreground_interval): arm/disarm, lock changes,
      setup/discovery, and their status polls.
    - BACKGROUND (min gap = background_interval): periodic alarm status,
      sentinel, air quality, lock status reads.

    Foreground requests preempt queued background work.  In-flight API calls
    are never cancelled — preemption happens between calls.
    """

    FOREGROUND = 0
    BACKGROUND = 1

    def __init__(
        self,
        foreground_interval: float = DEFAULT_FOREGROUND_INTERVAL,
        background_interval: float = DEFAULT_BACKGROUND_INTERVAL,
    ) -> None:
        self._intervals = {
            self.FOREGROUND: foreground_interval,
            self.BACKGROUND: background_interval,
        }
        self._lock = asyncio.Lock()
        self._last_api_time: float = 0
        self._pending_foreground: int = 0
        self._bg_event = asyncio.Event()
        self._bg_event.set()  # initially no foreground work pending

    async def submit(self, coro_fn, *args, priority: int = BACKGROUND):
        """Submit an API call and wait for its result.

        Args:
            coro_fn: Async callable (not a coroutine — the queue calls it).
            *args: Arguments passed to coro_fn.
            priority: FOREGROUND or BACKGROUND.

        Returns:
            The result of coro_fn(*args).

        Raises:
            Whatever coro_fn raises — exceptions propagate to the caller.
        """
        if priority == self.FOREGROUND:
            self._pending_foreground += 1
            self._bg_event.clear()

        try:
            while True:
                # Background callers wait while foreground work is pending
                if priority == self.BACKGROUND:
                    while self._pending_foreground > 0:
                        await self._bg_event.wait()

                async with self._lock:
                    # Background must re-check after acquiring lock — foreground
                    # may have arrived while we were waiting on the lock.
                    if priority == self.BACKGROUND and self._pending_foreground > 0:
                        # Release lock and loop back to yield to foreground
                        continue

                    interval = self._intervals[priority]
                    elapsed = time.monotonic() - self._last_api_time
                    if elapsed < interval:
                        delay = interval - elapsed
                        _LOGGER.debug(
                            "ApiQueue(%s) throttling %.1fs (%s) for %s",
                            id(self),
                            delay,
                            "fg" if priority == self.FOREGROUND else "bg",
                            getattr(coro_fn, "__name__", coro_fn),
                        )
                        await asyncio.sleep(delay)

                    try:
                        result = await coro_fn(*args)
                    finally:
                        self._last_api_time = time.monotonic()

                    return result
        finally:
            if priority == self.FOREGROUND:
                self._pending_foreground -= 1
                if self._pending_foreground == 0:
                    self._bg_event.set()
