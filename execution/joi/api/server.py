import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_settings
from llm import OllamaClient
from memory import MemoryStore

logger = logging.getLogger("joi.api")

settings = load_settings()

app = FastAPI(title="joi-api", version="0.1.0")

# Initialize Ollama client
llm = OllamaClient(
    base_url=settings.ollama_url,
    model=settings.ollama_model,
    timeout=60.0,
)

# Initialize memory store
memory = MemoryStore(
    db_path=os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db"),
    encryption_key=os.getenv("JOI_MEMORY_KEY"),
)

# Number of recent messages to include in context
CONTEXT_MESSAGE_COUNT = int(os.getenv("JOI_CONTEXT_MESSAGES", "10"))

# System prompt - loaded from file or fallback to default
SYSTEM_PROMPT_FILE = os.getenv("JOI_SYSTEM_PROMPT_FILE", "/var/lib/joi/system-prompt.txt")
DEFAULT_SYSTEM_PROMPT = """You are Joi, a helpful personal AI assistant. You are friendly, concise, and helpful.
Keep your responses brief and to the point unless asked for more detail.
You communicate via Signal messenger, so keep messages reasonably short."""


def _load_system_prompt() -> str:
    """Load system prompt from file, or use default if file doesn't exist."""
    try:
        with open(SYSTEM_PROMPT_FILE, "r") as f:
            prompt = f.read().strip()
            if prompt:
                return prompt
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Failed to load system prompt from %s: %s", SYSTEM_PROMPT_FILE, e)
    return DEFAULT_SYSTEM_PROMPT


SYSTEM_PROMPT = _load_system_prompt()


# --- Request/Response Models (per api-contracts.md) ---

class InboundSender(BaseModel):
    id: str
    transport_id: str
    display_name: Optional[str] = None


class InboundConversation(BaseModel):
    type: str  # "direct" or "group"
    id: str


class InboundContent(BaseModel):
    type: str  # "text", "reaction", etc.
    text: Optional[str] = None
    reaction: Optional[str] = None
    transport_native: Optional[Dict[str, Any]] = None


class InboundMessage(BaseModel):
    transport: str
    message_id: str
    sender: InboundSender
    conversation: InboundConversation
    priority: str = "normal"
    content: InboundContent
    timestamp: int
    quote: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class InboundResponse(BaseModel):
    status: str
    message_id: Optional[str] = None
    error: Optional[str] = None


# --- Endpoints ---

@app.get("/health")
def health():
    msg_count = memory.get_message_count()
    return {
        "status": "ok",
        "model": settings.ollama_model,
        "memory": {
            "messages": msg_count,
            "context_size": CONTEXT_MESSAGE_COUNT,
        }
    }


@app.post("/api/v1/message/inbound", response_model=InboundResponse)
def receive_message(msg: InboundMessage):
    """
    Receive a message from mesh proxy, process with LLM, send response back.
    """
    logger.info(
        "Received message_id=%s from=%s type=%s",
        msg.message_id,
        msg.sender.id,
        msg.content.type,
    )

    # Only handle text messages for now
    if msg.content.type != "text" or not msg.content.text:
        logger.info("Skipping non-text message type=%s", msg.content.type)
        return InboundResponse(status="ok", message_id=msg.message_id)

    user_text = msg.content.text.strip()
    if not user_text:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # Store inbound message
    memory.store_message(
        message_id=msg.message_id,
        direction="inbound",
        content_type=msg.content.type,
        content_text=user_text,
        timestamp=msg.timestamp,
        conversation_id=msg.conversation.id,
        reply_to_id=msg.quote.get("message_id") if msg.quote else None,
    )

    # Build conversation context from recent messages
    recent_messages = memory.get_recent_messages(
        limit=CONTEXT_MESSAGE_COUNT,
        conversation_id=msg.conversation.id,
    )

    # Convert to LLM chat format
    chat_messages = _build_chat_messages(recent_messages)

    # Generate response from LLM with conversation context
    logger.info("Generating LLM response with %d messages of context", len(chat_messages))
    llm_response = llm.chat(messages=chat_messages, system=SYSTEM_PROMPT)

    if llm_response.error:
        logger.error("LLM error: %s", llm_response.error)
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error=f"llm_error: {llm_response.error}",
        )

    response_text = llm_response.text.strip()
    if not response_text:
        logger.warning("LLM returned empty response")
        response_text = "I'm not sure how to respond to that."

    logger.info("LLM response: %s", response_text[:50])

    # Send response back via mesh
    outbound_message_id = str(uuid.uuid4())
    send_result = _send_to_mesh(
        recipient_id=msg.sender.id,
        recipient_transport_id=msg.sender.transport_id,
        conversation=msg.conversation,
        text=response_text,
        reply_to=msg.message_id,
    )

    if not send_result:
        logger.error("Failed to send response to mesh")
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error="mesh_send_failed",
        )

    # Store outbound message
    memory.store_message(
        message_id=outbound_message_id,
        direction="outbound",
        content_type="text",
        content_text=response_text,
        timestamp=int(time.time() * 1000),
        conversation_id=msg.conversation.id,
        reply_to_id=msg.message_id,
    )

    return InboundResponse(status="ok", message_id=msg.message_id)


def _build_chat_messages(messages: List) -> List[Dict[str, str]]:
    """Convert stored messages to LLM chat format."""
    chat_messages = []
    for msg in messages:
        if msg.content_text:
            role = "user" if msg.direction == "inbound" else "assistant"
            chat_messages.append({"role": role, "content": msg.content_text})
    return chat_messages


def _send_to_mesh(
    recipient_id: str,
    recipient_transport_id: str,
    conversation: InboundConversation,
    text: str,
    reply_to: Optional[str] = None,
) -> bool:
    """Send a message back to mesh for delivery via Signal."""
    url = f"{settings.mesh_url}/api/v1/message/outbound"

    payload = {
        "transport": "signal",
        "recipient": {
            "id": recipient_id,
            "transport_id": recipient_transport_id,
        },
        "priority": "normal",
        "delivery": {
            "target": conversation.type,
            "group_id": conversation.id if conversation.type == "group" else None,
        },
        "content": {
            "type": "text",
            "text": text,
        },
        "reply_to": reply_to,
        "escalated": False,
        "voice_response": False,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") == "ok":
            logger.info("Sent response to mesh successfully")
            return True
        else:
            logger.error("Mesh returned error: %s", data.get("error"))
            return False

    except Exception as exc:
        logger.error("Failed to send to mesh: %s", exc)
        return False


# --- Main ---

def main():
    import uvicorn

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Starting Joi API on %s:%d", settings.bind_host, settings.bind_port)
    logger.info("Ollama: %s (model: %s)", settings.ollama_url, settings.ollama_model)
    logger.info("Mesh: %s", settings.mesh_url)
    logger.info("Memory: %s (context: %d messages)",
                os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db"),
                CONTEXT_MESSAGE_COUNT)
    if os.path.exists(SYSTEM_PROMPT_FILE):
        logger.info("System prompt: %s", SYSTEM_PROMPT_FILE)
    else:
        logger.info("System prompt: default (no file at %s)", SYSTEM_PROMPT_FILE)

    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
