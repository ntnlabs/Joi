import os
from fastapi import FastAPI, HTTPException

from config import ensure_log_dir, load_settings
from jsonrpc_client import SignalJsonRpcClient

app = FastAPI(title="mesh-proxy", version="0.1.0")
settings = load_settings()
ensure_log_dir(settings.log_dir)
rpc = SignalJsonRpcClient(settings.signal_cli_socket)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/send_test")
def send_test(recipient: str, message: str):
    if os.getenv("MESH_ENABLE_TEST", "0") != "1":
        raise HTTPException(status_code=403, detail="Test endpoint disabled")
    if settings.signal_mode != "socket":
        raise HTTPException(status_code=409, detail="send_test requires socket mode")

    payload = {
        "account": os.getenv("SIGNAL_ACCOUNT", ""),
        "recipients": [recipient],
        "message": message,
    }
    if not payload["account"]:
        raise HTTPException(status_code=400, detail="SIGNAL_ACCOUNT not set")

    result = rpc.call("send", payload)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return {"status": "ok", "result": result.get("result")}
