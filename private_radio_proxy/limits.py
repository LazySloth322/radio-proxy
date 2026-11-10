"""In-memory rate limiting and concurrency limits.

All structures are bounded: sliding-window deques prune themselves and the
key maps are pruned periodically by a background task (see prune_idle), so a
large number of abusive IPs cannot grow memory indefinitely.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """Per-key sliding window counter (e.g. requests per IP per minute)."""

    def __init__(self, max_keys: int = 10000):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._max_keys = max_keys

    def allow(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        now = time.monotonic()
        dq = self._hits[key]
        cutoff = now - window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            dq.append(now)  # keep the attempt visible for pruning/limits
            return False
        dq.append(now)
        return True

    def prune_idle(self, window_seconds: float = 60.0) -> int:
        """Drop keys whose windows are fully expired. Returns keys kept."""
        now = time.monotonic()
        cutoff = now - window_seconds
        dead = [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]
        for k in dead:
            del self._hits[k]
        return len(self._hits)

    def maybe_prune(self, window_seconds: float = 60.0) -> None:
        """Automatic pruning when the map grows past max_keys."""
        if len(self._hits) > self._max_keys:
            self.prune_idle(window_seconds)


class ConcurrencyLimiter:
    """Named counters with per-key limits plus one global counter.

    Used for stream slots: global, per-IP and per-station limits share one
    instance with distinct key prefixes. try_acquire is atomic because the
    event loop never preempts between the check and the increment.
    """

    def __init__(self, max_keys: int = 10000):
        self._counts: dict[str, int] = defaultdict(int)
        self._max_keys = max_keys

    def try_acquire(self, key: str, limit: int) -> bool:
        if self._counts[key] >= limit:
            return False
        self._counts[key] += 1
        return True

    def release(self, key: str) -> None:
        if key in self._counts:
            self._counts[key] = max(0, self._counts[key] - 1)
            if self._counts[key] == 0:
                del self._counts[key]

    def current(self, key: str) -> int:
        return self._counts.get(key, 0)

    def prune(self) -> None:
        # zero-count keys are removed eagerly by release(); nothing to do,
        # but cap the map defensively if it ever grows.
        if len(self._counts) > self._max_keys:  # pragma: no cover
            for k in [k for k, v in self._counts.items() if v <= 0]:
                del self._counts[k]
