from collections import deque
from dataclasses import dataclass
import time
from typing import Deque, Dict, Tuple


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    reason: str = "ok"


class InboundRateLimiter:
    def __init__(self, max_per_hour: int, max_per_minute: int):
        self.max_per_hour = max_per_hour
        self.max_per_minute = max_per_minute
        self._events: Dict[str, Deque[int]] = {}

    def _get_queue(self, key: str) -> Deque[int]:
        queue = self._events.get(key)
        if queue is None:
            queue = deque()
            self._events[key] = queue
        return queue

    def check_and_add(self, key: str, now_ms: int | None = None) -> RateLimitResult:
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        q = self._get_queue(key)

        hour_ago = now_ms - 3600_000
        while q and q[0] < hour_ago:
            q.popleft()

        if len(q) >= self.max_per_hour:
            return RateLimitResult(allowed=False, reason="rate_limited_hour")

        minute_ago = now_ms - 60_000
        last_minute_count = 0
        for ts in reversed(q):
            if ts < minute_ago:
                break
            last_minute_count += 1

        if last_minute_count >= self.max_per_minute:
            return RateLimitResult(allowed=False, reason="rate_limited_minute")

        q.append(now_ms)
        return RateLimitResult(allowed=True)
