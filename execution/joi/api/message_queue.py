"""Priority message queue and outbound rate limiter."""

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

logger = logging.getLogger("joi.api.queue")


# --- Priority Message Queue ---

# Monotonic sequence counter for FIFO ordering within same priority
_message_seq = 0
_message_seq_lock = threading.Lock()


def _next_seq() -> int:
    """Get next sequence number for message ordering."""
    global _message_seq
    with _message_seq_lock:
        _message_seq += 1
        return _message_seq


@dataclass(order=True)
class PrioritizedMessage:
    """Message wrapper for priority queue. Lower priority number = higher priority."""
    priority: int
    sequence: int  # Monotonic counter for FIFO within same priority
    timestamp: float = field(compare=False)
    message_id: str = field(compare=False)
    handler: Callable = field(compare=False)
    result: Any = field(default=None, compare=False)
    error: Optional[str] = field(default=None, compare=False)
    done_event: threading.Event = field(default_factory=threading.Event, compare=False)
    cancelled: bool = field(default=False, compare=False)
    last_heartbeat: float = field(default_factory=time.time, compare=False)

    def heartbeat(self) -> None:
        """Signal that processing is still active (extends timeout)."""
        self.last_heartbeat = time.time()


class MessageQueue:
    """Global message queue with priority support and single worker."""

    PRIORITY_OWNER = 0  # Owner messages processed first
    PRIORITY_NORMAL = 1  # Other allowed senders
    MAX_QUEUE_SIZE = 100  # Prevent unbounded memory growth

    def __init__(self):
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=self.MAX_QUEUE_SIZE)
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._current_message_id: Optional[str] = None

    def start(self):
        """Start the worker thread."""
        if self._worker_thread is not None:
            return
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Message queue worker started", extra={"action": "queue_start"})

    def stop(self):
        """Stop the worker thread."""
        self._running = False
        # Put a sentinel to unblock the queue
        self._queue.put(PrioritizedMessage(
            priority=999,
            sequence=_next_seq(),
            timestamp=time.time(),
            message_id="__stop__",
            handler=lambda: None,
        ))
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None
        logger.info("Message queue worker stopped", extra={"action": "queue_stop"})

    # Heartbeat settings for dynamic timeout extension
    HEARTBEAT_INTERVAL = 30.0  # Check heartbeat every 30s
    MAX_EXTENSIONS = 5  # Max number of timeout extensions

    def enqueue(self, message_id: str, handler: Callable, is_owner: bool = False, timeout: float = 300.0) -> Any:
        """
        Add message to queue and wait for processing.

        Args:
            message_id: Unique message identifier
            handler: Function to call for processing (returns result).
                     Handler receives the PrioritizedMessage as argument for heartbeat support.
            is_owner: If True, gets priority processing
            timeout: Max seconds to wait for processing (can be extended via heartbeat)

        Returns:
            Result from handler

        Raises:
            TimeoutError: If processing takes too long (marks message as cancelled)
            Exception: If handler raises an error
        """
        priority = self.PRIORITY_OWNER if is_owner else self.PRIORITY_NORMAL
        msg = PrioritizedMessage(
            priority=priority,
            sequence=_next_seq(),
            timestamp=time.time(),
            message_id=message_id,
            handler=handler,
        )

        queue_size = self._queue.qsize()
        priority_label = "owner" if priority == self.PRIORITY_OWNER else "normal"
        logger.info("Queue ADD", extra={
            "message_id": message_id,
            "priority": priority_label,
            "queue_size": queue_size,
            "action": "queue_add"
        })

        try:
            self._queue.put(msg, timeout=5.0)  # Wait up to 5s if queue is full
        except queue.Full:
            logger.error("Queue FULL: dropping message", extra={
                "message_id": message_id,
                "max_size": self.MAX_QUEUE_SIZE,
                "action": "queue_full"
            })
            raise Exception("Message queue full, try again later")

        # Wait for processing with heartbeat-based timeout extension
        extensions = 0
        deadline = time.time() + timeout

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                # Timeout reached - mark as cancelled for deferred commit
                msg.cancelled = True
                logger.warning("Queue timeout, marking cancelled", extra={
                    "message_id": message_id,
                    "extensions": extensions,
                    "action": "queue_timeout"
                })
                raise TimeoutError(f"Message {message_id} processing timed out after {timeout}s (extensions: {extensions})")

            # Wait in intervals to check heartbeat
            wait_time = min(remaining, self.HEARTBEAT_INTERVAL)
            if msg.done_event.wait(timeout=wait_time):
                break  # Processing completed

            # Check if handler sent heartbeat recently (still working)
            if time.time() - msg.last_heartbeat < self.HEARTBEAT_INTERVAL:
                if extensions < self.MAX_EXTENSIONS:
                    deadline += self.HEARTBEAT_INTERVAL
                    extensions += 1
                    logger.debug("Extended timeout via heartbeat", extra={
                        "message_id": message_id,
                        "extensions": extensions,
                        "action": "timeout_extend"
                    })

        if msg.error:
            raise Exception(msg.error)

        return msg.result

    def _worker_loop(self):
        """Process messages from queue sequentially."""
        while self._running:
            try:
                msg = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if msg.message_id == "__stop__":
                break

            self._current_message_id = msg.message_id
            start_time = time.time()
            priority_label = "owner" if msg.priority == self.PRIORITY_OWNER else "normal"
            logger.info("Queue START", extra={
                "message_id": msg.message_id,
                "priority": priority_label,
                "action": "queue_process_start"
            })

            try:
                # Pass message to handler for heartbeat support
                msg.result = msg.handler(msg)
            except Exception as e:
                logger.error("Queue ERROR", extra={
                    "message_id": msg.message_id,
                    "error": str(e),
                    "action": "queue_error"
                })
                msg.error = str(e)
            finally:
                elapsed = time.time() - start_time
                logger.info("Queue DONE", extra={
                    "message_id": msg.message_id,
                    "duration_ms": int(elapsed * 1000),
                    "cancelled": msg.cancelled,
                    "action": "queue_process_done"
                })
                self._current_message_id = None
                msg.done_event.set()

    def get_queue_size(self) -> int:
        """Get current queue size."""
        return self._queue.qsize()


# --- Outbound Rate Limiter ---

class OutboundRateLimiter:
    """
    Rate limiter for outbound messages to mesh.

    Uses sliding window to track messages per hour.
    Critical messages bypass rate limiting.
    """

    DEFAULT_MAX_PER_HOUR = 120  # Configurable via env

    def __init__(self, max_per_hour: Optional[int] = None):
        self._max_per_hour = max_per_hour or int(os.getenv("JOI_OUTBOUND_MAX_PER_HOUR", str(self.DEFAULT_MAX_PER_HOUR)))
        self._timestamps: List[float] = []
        self._lock = threading.Lock()
        self._blocked_count = 0

    def _cleanup_old(self, now: float) -> None:
        """Remove timestamps older than 1 hour."""
        one_hour_ago = now - 3600
        self._timestamps = [ts for ts in self._timestamps if ts > one_hour_ago]

    def check_and_record(self, is_critical: bool = False) -> tuple[bool, str]:
        """
        Check if send is allowed and record it.

        Args:
            is_critical: If True, bypass rate limiting

        Returns:
            (allowed, reason)
        """
        now = time.time()

        with self._lock:
            self._cleanup_old(now)
            current_count = len(self._timestamps)

            # Critical messages always allowed
            if is_critical:
                self._timestamps.append(now)
                return True, "critical_bypass"

            # Check rate limit
            if current_count >= self._max_per_hour:
                self._blocked_count += 1
                logger.warning("Outbound rate limit", extra={
                    "current_count": current_count,
                    "max_per_hour": self._max_per_hour,
                    "blocked_total": self._blocked_count,
                    "action": "rate_limited"
                })
                return False, "rate_limited"

            self._timestamps.append(now)
            return True, "allowed"

    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        now = time.time()
        with self._lock:
            self._cleanup_old(now)
            return {
                "current_hour_count": len(self._timestamps),
                "max_per_hour": self._max_per_hour,
                "blocked_total": self._blocked_count,
            }
