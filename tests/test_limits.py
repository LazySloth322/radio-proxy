"""Rate limiter and concurrency limiter tests."""
import unittest
from types import SimpleNamespace
from unittest import mock

from private_radio_proxy.limits import ConcurrencyLimiter, SlidingWindowRateLimiter


class TestSlidingWindow(unittest.TestCase):
    def test_allows_within_limit_blocks_over(self):
        limiter = SlidingWindowRateLimiter()
        clock = [1000.0]
        with mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(limiter.allow("ip1", 2, 60))
            self.assertTrue(limiter.allow("ip1", 2, 60))
            self.assertFalse(limiter.allow("ip1", 2, 60))
            # window slides
            clock[0] += 61
            self.assertTrue(limiter.allow("ip1", 2, 60))

    def test_keys_are_independent(self):
        limiter = SlidingWindowRateLimiter()
        self.assertTrue(limiter.allow("a", 1, 60))
        self.assertTrue(limiter.allow("b", 1, 60))
        self.assertFalse(limiter.allow("a", 1, 60))

    def test_prune_idle(self):
        limiter = SlidingWindowRateLimiter()
        clock = [1000.0]
        with mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            limiter.allow("a", 10, 60)
            limiter.allow("b", 10, 60)
            clock[0] += 120
            kept = limiter.prune_idle(60)
            self.assertEqual(kept, 0)
            self.assertEqual(len(limiter._hits), 0)


class TestConcurrency(unittest.TestCase):
    def test_acquire_release(self):
        lim = ConcurrencyLimiter()
        self.assertTrue(lim.try_acquire("global", 2))
        self.assertTrue(lim.try_acquire("global", 2))
        self.assertFalse(lim.try_acquire("global", 2))
        lim.release("global")
        self.assertTrue(lim.try_acquire("global", 2))

    def test_release_cleans_up_zero_entries(self):
        lim = ConcurrencyLimiter()
        lim.try_acquire("ip:1.2.3.4", 3)
        lim.release("ip:1.2.3.4")
        self.assertEqual(lim._counts, {})

    def test_release_floor_zero(self):
        lim = ConcurrencyLimiter()
        lim.release("nope")  # must not raise / go negative
        self.assertEqual(lim.current("nope"), 0)


class TestStreamSlots(unittest.TestCase):
    """Regression: acquire_stream_slots must return the acquired keys so the
    caller can release them; returning [] on success leaked every counter and
    permanently 429'd streams after a few play/stop cycles (found 2026-11)."""

    def _ctx(self):
        from private_radio_proxy import stream_proxy
        limits = SimpleNamespace(max_concurrent_streams_global=2,
                                 max_concurrent_streams_per_ip=2,
                                 max_concurrent_streams_per_station=2)
        conc = ConcurrencyLimiter()
        ctx = SimpleNamespace(cfg=SimpleNamespace(limits=limits), stream_conc=conc)
        return stream_proxy, ctx, conc

    def test_acquire_returns_keys_and_release_restores(self):
        stream_proxy, ctx, conc = self._ctx()
        station = SimpleNamespace(id="s1")
        ok, keys = stream_proxy.acquire_stream_slots(ctx, station, "1.2.3.4")
        self.assertTrue(ok)
        self.assertEqual(len(keys), 3)  # global + ip + station
        stream_proxy.release_stream_slots(ctx, keys)
        self.assertEqual(conc._counts, {})  # nothing leaked

    def test_repeated_cycles_do_not_exhaust_limits(self):
        stream_proxy, ctx, conc = self._ctx()
        station = SimpleNamespace(id="s1")
        for _ in range(5):
            ok, keys = stream_proxy.acquire_stream_slots(ctx, station, "1.2.3.4")
            self.assertTrue(ok)
            stream_proxy.release_stream_slots(ctx, keys)
        ok, keys = stream_proxy.acquire_stream_slots(ctx, station, "1.2.3.4")
        self.assertTrue(ok)  # a fresh listener is never blocked by old leaks
        stream_proxy.release_stream_slots(ctx, keys)

    def test_failed_acquire_releases_partial_slots(self):
        stream_proxy, ctx, conc = self._ctx()
        station = SimpleNamespace(id="s1")
        conc.try_acquire("global", 2)
        conc.try_acquire("global", 2)  # global exhausted
        ok, keys = stream_proxy.acquire_stream_slots(ctx, station, "5.6.7.8")
        self.assertFalse(ok)
        self.assertEqual(keys, [])
        self.assertEqual(conc.current("ip:5.6.7.8"), 0)
        self.assertEqual(conc.current("station:s1"), 0)


if __name__ == "__main__":
    unittest.main()
