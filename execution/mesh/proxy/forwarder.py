from typing import Any, Dict
import os

import httpx


def forward_to_joi(payload: Dict[str, Any]) -> None:
    if os.getenv("MESH_ENABLE_FORWARD", "0") != "1":
        return

    url = os.getenv("MESH_JOI_INBOUND_URL", "http://joi:8443/api/v1/message/inbound")
    timeout_s = float(os.getenv("MESH_FORWARD_TIMEOUT", "120"))

    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
