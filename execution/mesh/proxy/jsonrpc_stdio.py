import json
import subprocess
import threading
from queue import Empty, Queue
from typing import Any, Dict, List, Optional


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
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("Failed to open stdio pipes for jsonRpc process")
        self._next_id = 1
        self._lock = threading.Lock()
        self._responses: Dict[int, Queue] = {}
        self._notifications: Queue = Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        if self._proc.stdout is None:
            return
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

    def call(self, method: str, params: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
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
        self._proc.stdin.write(json.dumps(request) + "\n")
        self._proc.stdin.flush()

        try:
            response = response_queue.get(timeout=timeout)
            return response
        except Empty as exc:
            raise RuntimeError(f"Timeout waiting for JSON-RPC response: {method}") from exc
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
