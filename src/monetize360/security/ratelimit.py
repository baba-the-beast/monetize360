"""Token bucket per key and per tenant in Redis; per-replica fallback if Redis is down.

M4: only the per-replica bucket exists (no Redis yet), so with N replicas a key can reach N x its limit.
M5 puts the counter in Redis and keeps this as the fallback."""
from __future__ import annotations

import time
from typing import Callable

MAX_BUCKETS = 50_000


class RateLimiter:
    """Each key may burst up to one second's worth of requests, refilled at `rps` per second."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._buckets: dict[str, tuple[float, float]] = {}        # key -> (tokens, last refill time)

    def allow(self, key: str, rps: int) -> float:
        """0.0 when the request may proceed, else the seconds to wait before retrying."""
        if rps <= 0:
            return 1.0
        now = self._monotonic()
        tokens, last = self._buckets.get(key, (float(rps), now))
        tokens = min(float(rps), tokens + (now - last) * rps)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / rps
        if len(self._buckets) >= MAX_BUCKETS and key not in self._buckets:
            self._buckets.clear()
        self._buckets[key] = (tokens - 1.0, now)
        return 0.0
