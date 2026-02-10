import logging
import os
import time
from typing import Any, Dict, List

from config import load_settings
from forwarder import forward_to_joi
from jsonrpc_stdio import JsonRpcStdioClient


logger = logging.getLogger("mesh.signal_worker")


def _receive_messages(rpc: JsonRpcStdioClient, account: str, timeout_s: int) -> List[Dict[str, Any]]:
    params = {"account": account, "timeout": timeout_s}
    result = rpc.call("receive", params)
    if "error" in result:
        raise RuntimeError(result["error"])
    rpc_result = result.get("result")
    if isinstance(rpc_result, list):
        return rpc_result
    return []


def _extract_messages(notifications: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for item in notifications:
        if item.get("method") != "receive":
            continue
        params = item.get("params")
        if isinstance(params, dict):
            messages.append(params)
        elif isinstance(params, list):
            for entry in params:
                if isinstance(entry, dict):
                    messages.append(entry)
    return messages


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

    logger.info("Signal worker started (manual receive)")

    try:
        while True:
            try:
                inline_messages = _receive_messages(rpc, account, timeout_s)
                notification_messages = _extract_messages(rpc.pop_all_notifications())
                messages = inline_messages + notification_messages

                if messages:
                    logger.info("Received %d message(s) from Signal", len(messages))
                for msg in messages:
                    payload = {"transport": "signal", "raw": msg}
                    forward_to_joi(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("signal_worker error: %s", exc)

            time.sleep(poll_seconds)
    finally:
        rpc.close()


if __name__ == "__main__":
    main()
