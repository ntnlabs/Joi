import os
import time
from typing import Any, Dict, List

from config import load_settings
from forwarder import forward_to_joi
from jsonrpc_stdio import JsonRpcStdioClient


def _receive_messages(rpc: JsonRpcStdioClient, account: str, timeout_s: int) -> List[Dict[str, Any]]:
    params = {"account": account, "timeout": timeout_s}
    result = rpc.call("receive", params)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result.get("result", [])


def main() -> None:
    settings = load_settings()
    account = os.getenv("SIGNAL_ACCOUNT", "")
    if not account:
        raise SystemExit("SIGNAL_ACCOUNT not set")

    poll_seconds = int(os.getenv("MESH_SIGNAL_POLL_SECONDS", "5"))
    timeout_s = int(os.getenv("MESH_SIGNAL_TIMEOUT", "10"))

    rpc = JsonRpcStdioClient(
        [
            "/usr/local/bin/signal-cli",
            "--config",
            "/var/lib/signal-cli",
            "jsonRpc",
            "--receive-mode=manual",
        ]
    )

    while True:
        try:
            messages = _receive_messages(rpc, account, timeout_s)
            for msg in messages:
                payload = {"transport": "signal", "raw": msg}
                forward_to_joi(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"signal_worker error: {exc}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
