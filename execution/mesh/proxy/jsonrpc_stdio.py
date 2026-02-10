from collections import deque
import json
import subprocess
import threading
from queue import Empty, Queue
import time
from typing import Any, Deque, Dict, List, Optional


class JsonRpcStdioClient:
    def __init__(self, command: List[str]):
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            raise RuntimeError("Failed to open stdio pipes for jsonRpc process")
        self._next_id = 1
        self._lock = threading.Lock()
        self._responses: Dict[int, Queue] = {}
        self._notifications: Queue = Queue()
        self._stderr_lines: Deque[str] = deque(maxlen=50)
        self._stdout_closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr_loop, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def _read_loop(self) -> None:
        if self._proc.stdout is None:
            return
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue

                message_id = message.get("id")
                if isinstance(message_id, int):
                    with self._lock:
                        response_queue = self._responses.get(message_id)
                    if response_queue is not None:
                        response_queue.put(message)
                    continue

                if "method" in message:
                    self._notifications.put(message)
        finally:
            self._stdout_closed.set()

    def _read_stderr_loop(self) -> None:
        if self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            line = line.strip()
            if not line:
                continue
            with self._lock:
                self._stderr_lines.append(line)

    def _stderr_summary(self) -> str:
        with self._lock:
            if not self._stderr_lines:
                return "<no stderr>"
            return " | ".join(self._stderr_lines)

    def _assert_running(self, context: str) -> None:
        return_code = self._proc.poll()
        if return_code is not None:
            raise RuntimeError(
                f"jsonRpc process exited ({return_code}) during {context}. stderr: {self._stderr_summary()}"
            )

    def call(self, method: str, params: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
        self._assert_running(f"call:{method} (pre-send)")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: Queue = Queue(maxsize=1)
            self._responses[request_id] = response_queue

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            self._proc.stdin.write(json.dumps(request) + "\n")
            self._proc.stdin.flush()
        except BrokenPipeError as exc:
            self._assert_running(f"call:{method} (send)")
            raise RuntimeError(f"Failed to send JSON-RPC request: {method}") from exc

        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Timeout waiting for JSON-RPC response: {method}. stderr: {self._stderr_summary()}"
                    )
                try:
                    response = response_queue.get(timeout=min(remaining, 0.5))
                    return response
                except Empty:
                    if self._stdout_closed.is_set():
                        self._assert_running(f"call:{method} (stdout closed)")
                        raise RuntimeError(
                            f"jsonRpc stdout closed while waiting for response: {method}. "
                            f"stderr: {self._stderr_summary()}"
                        )
                    self._assert_running(f"call:{method} (wait)")
        finally:
            with self._lock:
                self._responses.pop(request_id, None)

    def pop_notification(self, timeout: float = 0.0) -> Optional[Dict[str, Any]]:
        try:
            return self._notifications.get(timeout=timeout)
        except Empty:
            return None

    def pop_all_notifications(self) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        while True:
            message = self.pop_notification(timeout=0.0)
            if message is None:
                return messages
            messages.append(message)

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
