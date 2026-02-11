import logging
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_settings, get_prompt_for_conversation, ensure_prompts_dir
from llm import OllamaClient
from memory import MemoryConsolidator, MemoryStore

logger = logging.getLogger("joi.api")

settings = load_settings()

app = FastAPI(title="joi-api", version="0.1.0")

# Initialize Ollama client
LLM_TIMEOUT = float(os.getenv("JOI_LLM_TIMEOUT", "180"))
llm = OllamaClient(
    base_url=settings.ollama_url,
    model=settings.ollama_model,
    timeout=LLM_TIMEOUT,
)

# Initialize memory store
memory = MemoryStore(
    db_path=os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db"),
    encryption_key=os.getenv("JOI_MEMORY_KEY"),
)

# Number of recent messages to include in context
CONTEXT_MESSAGE_COUNT = int(os.getenv("JOI_CONTEXT_MESSAGES", "10"))

# Memory consolidation settings
CONSOLIDATION_SILENCE_HOURS = float(os.getenv("JOI_CONSOLIDATION_SILENCE_HOURS", "1"))
CONSOLIDATION_MAX_MESSAGES = int(os.getenv("JOI_CONSOLIDATION_MAX_MESSAGES", "200"))
CONSOLIDATION_ARCHIVE = os.getenv("JOI_CONSOLIDATION_ARCHIVE", "0") == "1"  # Default: delete

# RAG settings
RAG_ENABLED = os.getenv("JOI_RAG_ENABLED", "1") == "1"  # Default: enabled
RAG_MAX_TOKENS = int(os.getenv("JOI_RAG_MAX_TOKENS", "500"))  # Max tokens for RAG context

# Initialize memory consolidator
consolidator = MemoryConsolidator(memory=memory, llm_client=llm)

# Ensure prompts directory exists
ensure_prompts_dir()

# Joi addressing patterns for group messages
# Matches: "Joi", "joi", "Joi,", "joi:", "@Joi", etc. at start of message or standalone
JOI_ADDRESS_PATTERNS = [
    r"^@?joi[,:\s]",           # "Joi," "joi:" "@joi " at start
    r"^@?joi$",                # Just "Joi" or "@Joi" alone
    r"\s@joi[\s,:]",           # "@joi" in the middle
    r"\s@joi$",                # "@joi" at the end
]
JOI_ADDRESS_REGEX = re.compile("|".join(JOI_ADDRESS_PATTERNS), re.IGNORECASE)


def _is_addressing_joi(text: str) -> bool:
    """Check if the message is addressing Joi directly."""
    return bool(JOI_ADDRESS_REGEX.search(text))


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
    store_only: bool = False  # If True, store for context but don't respond


class InboundResponse(BaseModel):
    status: str
    message_id: Optional[str] = None
    error: Optional[str] = None


# --- Endpoints ---

@app.get("/health")
def health():
    msg_count = memory.get_message_count()
    facts = memory.get_facts(min_confidence=0.0, limit=1000)
    summaries = memory.get_recent_summaries(days=30, limit=100)
    knowledge_sources = memory.get_knowledge_sources()
    knowledge_chunks = sum(s["chunk_count"] for s in knowledge_sources)
    return {
        "status": "ok",
        "model": settings.ollama_model,
        "memory": {
            "messages": msg_count,
            "facts": len(facts),
            "summaries": len(summaries),
            "context_size": CONTEXT_MESSAGE_COUNT,
        },
        "rag": {
            "enabled": RAG_ENABLED,
            "sources": len(knowledge_sources),
            "chunks": knowledge_chunks,
        }
    }


@app.post("/api/v1/message/inbound", response_model=InboundResponse)
def receive_message(msg: InboundMessage):
    """
    Receive a message from mesh proxy, process with LLM, send response back.

    For group messages:
    - store_only=True: Store for context but don't respond (non-allowed senders)
    - store_only=False: Check if Joi is addressed before responding
    """
    logger.info(
        "Received message_id=%s from=%s type=%s convo=%s store_only=%s",
        msg.message_id,
        msg.sender.transport_id,
        msg.content.type,
        msg.conversation.type,
        msg.store_only,
    )

    # Only handle text messages for now
    if msg.content.type != "text" or not msg.content.text:
        logger.info("Skipping non-text message type=%s", msg.content.type)
        return InboundResponse(status="ok", message_id=msg.message_id)

    user_text = msg.content.text.strip()
    if not user_text:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # Store inbound message (always store for context)
    memory.store_message(
        message_id=msg.message_id,
        direction="inbound",
        content_type=msg.content.type,
        content_text=user_text,
        timestamp=msg.timestamp,
        conversation_id=msg.conversation.id,
        reply_to_id=msg.quote.get("message_id") if msg.quote else None,
        sender_id=msg.sender.transport_id,
        sender_name=msg.sender.display_name,
    )

    # Determine if we should respond
    should_respond = True

    if msg.store_only:
        # Non-allowed sender in group - store only, no response
        logger.info("Message stored for context only (store_only=True)")
        should_respond = False
    elif msg.conversation.type == "group":
        # Group message from allowed sender - only respond if Joi is addressed
        if _is_addressing_joi(user_text):
            logger.info("Joi addressed in group message, will respond")
            should_respond = True
        else:
            logger.info("Joi not addressed in group message, storing only")
            should_respond = False

    if not should_respond:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # Build conversation context from recent messages
    recent_messages = memory.get_recent_messages(
        limit=CONTEXT_MESSAGE_COUNT,
        conversation_id=msg.conversation.id,
    )

    # Convert to LLM chat format (with sender prefix for groups)
    is_group = msg.conversation.type == "group"
    chat_messages = _build_chat_messages(recent_messages, is_group=is_group)

    # Get per-conversation system prompt and enrich it
    base_prompt = get_prompt_for_conversation(
        conversation_type=msg.conversation.type,
        conversation_id=msg.conversation.id,
        sender_id=msg.sender.transport_id,
    )
    enriched_prompt = _build_enriched_prompt(base_prompt, user_text)

    # Generate response from LLM with conversation context
    logger.info("Generating LLM response with %d messages of context", len(chat_messages))
    llm_response = llm.chat(messages=chat_messages, system=enriched_prompt)

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

    # Check if memory consolidation needed (runs async-ish, only if conditions met)
    _maybe_run_consolidation()

    return InboundResponse(status="ok", message_id=msg.message_id)


def _build_chat_messages(messages: List, is_group: bool = False) -> List[Dict[str, str]]:
    """Convert stored messages to LLM chat format.

    For group conversations, includes sender name prefix so Joi knows who said what.
    """
    chat_messages = []
    for msg in messages:
        if msg.content_text:
            role = "user" if msg.direction == "inbound" else "assistant"

            if role == "user" and is_group:
                # For group messages, prefix with sender name/id
                sender = msg.sender_name or msg.sender_id or "Unknown"
                content = f"[{sender}]: {msg.content_text}"
            else:
                content = msg.content_text

            chat_messages.append({"role": role, "content": content})
    return chat_messages


def _build_enriched_prompt(base_prompt: str, user_message: Optional[str] = None) -> str:
    """Build system prompt enriched with user facts, summaries, and RAG context."""
    parts = [base_prompt]

    # Add user facts
    facts_text = memory.get_facts_as_text(min_confidence=0.6)
    if facts_text:
        parts.append("\n\n" + facts_text)

    # Add recent conversation summaries
    summaries_text = memory.get_summaries_as_text(days=7)
    if summaries_text:
        parts.append("\n\n" + summaries_text)

    # Add RAG context if enabled and user message provided
    if RAG_ENABLED and user_message:
        rag_context = memory.get_knowledge_as_context(user_message, max_tokens=RAG_MAX_TOKENS)
        if rag_context:
            parts.append("\n\n" + rag_context)
            logger.debug("Added RAG context for query: %s", user_message[:50])

    return "".join(parts)


def _maybe_run_consolidation() -> None:
    """Run memory consolidation if needed (after silence or message threshold)."""
    try:
        result = consolidator.run_consolidation(
            silence_threshold_ms=int(CONSOLIDATION_SILENCE_HOURS * 3600 * 1000),
            max_messages_before_consolidation=CONSOLIDATION_MAX_MESSAGES,
            keep_recent_messages=CONTEXT_MESSAGE_COUNT,
            archive_instead_of_delete=CONSOLIDATION_ARCHIVE,
        )
        if result["ran"]:
            action = "archived" if CONSOLIDATION_ARCHIVE else "deleted"
            logger.info(
                "Memory consolidation: facts=%d, summarized=%d, %s=%d",
                result["facts_extracted"],
                result["messages_summarized"],
                action,
                result["messages_removed"],
            )
    except Exception as e:
        logger.error("Consolidation error: %s", e)


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

    from config.prompts import PROMPTS_DIR

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
    logger.info("Prompts directory: %s", PROMPTS_DIR)

    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
