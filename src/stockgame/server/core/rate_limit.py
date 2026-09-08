"""In-process rate limiting.

A sliding-window counter keyed by an arbitrary string (client IP for auth,
user id for orders). Single-process only -- if the server is ever scaled
horizontally this is the piece that moves to Redis, which is why the call
sites depend on the :meth:`RateLimiter.check` interface rather than the
internals.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(slots=True)
class RateLimitResult:
    allowed: bool
    retry_after: float = 0.0
    remaining: int = 0


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, cost: int = 1) -> RateLimitResult:
        """Record an attempt and report whether it is permitted."""
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) + cost > self.limit:
            retry_after = max(0.0, bucket[0] + self.window - now) if bucket else self.window
            return RateLimitResult(allowed=False, retry_after=retry_after, remaining=0)
        for _ in range(cost):
            bucket.append(now)
        return RateLimitResult(allowed=True, remaining=self.limit - len(bucket))

    def reset(self, key: str) -> None:
        """Clear a key -- called after a successful login so a legitimate user
        who fumbled their password is not left throttled."""
        self._hits.pop(key, None)

    def prune(self) -> int:
        """Drop empty buckets so long-running servers do not leak memory."""
        now = time.monotonic()
        cutoff = now - self.window
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        return len(stale)
