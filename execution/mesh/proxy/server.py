import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from config import ensure_log_dir, load_settings
from jsonrpc_client import SignalJsonRpcClient
from rate_limiter import InboundRateLimiter


logger = logging.getLogger("mesh.server")

app = FastAPI(title="mesh-proxy", version="0.1.0")
settings = load_settings()
ensure_log_dir(settings.log_dir)

# Socket client for outbound (requires signal-cli daemon mode)
_rpc: Optional[SignalJsonRpcClient] = None


def get_rpc() -> SignalJsonRpcClient:
    global _rpc
    if _rpc is None:
        _rpc = SignalJsonRpcClient(settings.signal_cli_socket)
    return _rpc


class OutboundRecipient(BaseModel):
    id: str  # Canonical identity (e.g., "owner")
    transport_id: str  # Transport-native identifier (e.g., "+1555...")


class OutboundDelivery(BaseModel):
    target: str = "direct"  # "direct" or "group"
    group_id: Optional[str] = None


class OutboundContent(BaseModel):
    type: str = "text"  # "text" or "voice" (voice is future)
    text: str


class OutboundMessage(BaseModel):
    """Message from Joi to send via Signal (per api-contracts.md)."""
    transport: str = "signal"
    recipient: OutboundRecipient
    priority: str = "normal"  # "normal" or "critical"
    delivery: OutboundDelivery = OutboundDelivery()
    conversation_id: Optional[str] = None
    content: OutboundContent
    reply_to: Optional[str] = None  # message_id to quote
    escalated: bool = False  # True if LLM judged urgent
    voice_response: bool = False  # Future: TTS


class OutboundResponseData(BaseModel):
    message_id: Optional[str] = None
    transport: str = "signal"
    sent_at: Optional[int] = None
    delivered: bool = False


class OutboundResponse(BaseModel):
    status: str
    request_id: Optional[str] = None
    timestamp: Optional[int] = None
    data: Optional[OutboundResponseData] = None
    error: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


# Outbound rate limiters (per api-contracts.md)
# direct: 60/hour, critical (LLM-escalated): 120/hour, critical (event): unlimited
_outbound_rate_limiter = InboundRateLimiter(max_per_hour=60, max_per_minute=10)
_outbound_critical_limiter = InboundRateLimiter(max_per_hour=120, max_per_minute=20)


@app.post("/api/v1/message/outbound", response_model=OutboundResponse)
def send_outbound(msg: OutboundMessage):
    """
    Send a message from Joi to Signal.

    This endpoint is called by Joi API to send responses back to users.
    Requires signal-cli running in daemon mode with socket.
    """
    request_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)

    account = os.getenv("SIGNAL_ACCOUNT", "")
    if not account:
        raise HTTPException(status_code=500, detail="SIGNAL_ACCOUNT not configured")

    if settings.signal_mode != "socket":
        raise HTTPException(
            status_code=503,
            detail="Outbound requires socket mode (signal-cli daemon)"
        )

    # Validate transport
    if msg.transport != "signal":
        return OutboundResponse(
            status="error",
            request_id=request_id,
            timestamp=now_ms,
            error=f"Unsupported transport: {msg.transport}"
        )

    # Validate content type
    if msg.content.type != "text":
        return OutboundResponse(
            status="error",
            request_id=request_id,
            timestamp=now_ms,
            error=f"Unsupported content type: {msg.content.type}"
        )

    # Validate text length (max 2048 per api-contracts.md)
    if len(msg.content.text) > 2048:
        return OutboundResponse(
            status="error",
            request_id=request_id,
            timestamp=now_ms,
            error="Text exceeds maximum length (2048)"
        )

    # Rate limiting based on priority
    sender_key = f"outbound:{msg.recipient.id}"
    if msg.priority == "critical":
        if msg.escalated:
            # LLM-escalated critical: rate limited at 120/hr
            limit_result = _outbound_critical_limiter.check_and_add(sender_key, now_ms)
            if not limit_result.allowed:
                logger.warning("Rate limited critical (escalated) to %s", msg.recipient.id)
                return OutboundResponse(
                    status="error",
                    request_id=request_id,
                    timestamp=now_ms,
                    error="rate_limited_critical"
                )
        # else: event-triggered critical - no rate limit
    else:
        # Normal priority: 60/hr
        limit_result = _outbound_rate_limiter.check_and_add(sender_key, now_ms)
        if not limit_result.allowed:
            logger.warning("Rate limited outbound to %s", msg.recipient.id)
            return OutboundResponse(
                status="error",
                request_id=request_id,
                timestamp=now_ms,
                error="rate_limited"
            )

    # Build signal-cli send payload
    payload: Dict[str, Any] = {
        "account": account,
        "message": msg.content.text,
    }

    if msg.delivery.target == "group":
        if not msg.delivery.group_id:
            return OutboundResponse(
                status="error",
                request_id=request_id,
                timestamp=now_ms,
                error="group_id required for group delivery"
            )
        payload["groupId"] = msg.delivery.group_id
    else:
        payload["recipients"] = [msg.recipient.transport_id]

    # Handle reply/quote
    if msg.reply_to:
        # reply_to is a message_id; signal-cli uses timestamp for quotes
        # For now, we'd need to look up the timestamp - simplified for PoC
        logger.debug("reply_to not yet implemented: %s", msg.reply_to)

    try:
        rpc = get_rpc()
        result = rpc.call("send", payload)
    except Exception as exc:
        logger.error("Signal send failed: %s", exc)
        return OutboundResponse(
            status="error",
            request_id=request_id,
            timestamp=now_ms,
            error=str(exc)
        )

    if "error" in result:
        logger.warning("Signal send error: %s", result["error"])
        return OutboundResponse(
            status="error",
            request_id=request_id,
            timestamp=now_ms,
            error=str(result["error"])
        )

    # Extract message details from result
    sent_at = None
    message_id = None
    res = result.get("result")
    if isinstance(res, dict):
        sent_at = res.get("timestamp")
    elif isinstance(res, list) and res:
        sent_at = res[0].get("timestamp")

    if sent_at:
        message_id = str(sent_at)  # Signal uses timestamp as message ID

    logger.info("Sent message to %s (priority=%s)", msg.recipient.id, msg.priority)
    return OutboundResponse(
        status="ok",
        request_id=request_id,
        timestamp=now_ms,
        data=OutboundResponseData(
            message_id=message_id,
            transport="signal",
            sent_at=sent_at,
            delivered=False
        )
    )


@app.post("/send_test")
def send_test(recipient: str, message: str):
    """Legacy test endpoint - use /api/v1/message/outbound instead."""
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

    rpc = get_rpc()
    result = rpc.call("send", payload)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return {"status": "ok", "result": result.get("result")}
