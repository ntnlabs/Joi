import json
import socket
from typing import Any, Dict


class SignalJsonRpcClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        payload = json.dumps(request).encode("utf-8")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.socket_path)
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)

            response_bytes = s.recv(65536)

        if not response_bytes:
            raise RuntimeError("No response from signal-cli JSON-RPC socket")

        return json.loads(response_bytes.decode("utf-8"))
