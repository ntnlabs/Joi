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

# Default names that Joi responds to in group messages (comma-separated)
JOI_NAMES_DEFAULT = [name.strip() for name in os.getenv("JOI_NAMES", "Joi").split(",") if name.strip()]


def _build_address_regex(names: list) -> re.Pattern:
    """Build regex pattern for addressing detection from list of names."""
    patterns = []
    for name in names:
        # Escape special regex characters in name
        escaped = re.escape(name)
        patterns.extend([
            rf"^@?{escaped}[,:\s]",    # "Name," "name:" "@name " at start
            rf"^@?{escaped}$",          # Just "Name" or "@Name" alone
            rf"\s@{escaped}[\s,:]",     # "@name" in the middle
            rf"\s@{escaped}$",          # "@name" at the end
        ])
    return re.compile("|".join(patterns), re.IGNORECASE)


# Cache for compiled regexes per name list
_address_regex_cache: Dict[tuple, re.Pattern] = {}


# Patterns for "remember this" requests (English only for now)
# Must be explicit fact statements about the user, not general statements
REMEMBER_PATTERNS = [
    r"remember\s+that\s+(?:i|my)\s+(.+)",  # "remember that I..." or "remember that my..."
    r"don'?t\s+forget\s+that\s+(?:i|my)\s+(.+)",  # "don't forget that I/my..."
    r"keep\s+in\s+mind\s+that\s+(?:i|my)\s+(.+)",  # "keep in mind that I/my..."
    r"^my\s+name\s+is\s+(\w+)",  # "my name is X" at start
    r"^i'?m\s+called\s+(\w+)",  # "I'm called X" at start
    r"^my\s+(\w+)\s+is\s+(.+)",  # "my birthday is March 5th" at start
    r"^i\s+(?:really\s+)?(?:like|love|hate|prefer)\s+(.+)",  # "I like X" at start
]
REMEMBER_REGEX = re.compile("|".join(REMEMBER_PATTERNS), re.IGNORECASE)


def _detect_remember_request(text: str) -> Optional[str]:
    """Check if user is asking Joi to remember something. Returns the thing to remember."""
    match = REMEMBER_REGEX.search(text)
    if match:
        # Return the first non-None group
        for group in match.groups():
            if group:
                return group.strip()
    return None


def _extract_and_save_fact(text: str, remember_what: str) -> Optional[str]:
    """Use LLM to extract a structured fact and save it. Returns confirmation message."""
    prompt = f"""The user said: "{text}"
They want me to remember: "{remember_what}"

Extract this as a fact with these exact fields:
- category: one of "personal", "preference", "relationship", "work", "routine", "interest"
- key: short identifier (2-3 words max)
- value: the fact itself

Return ONLY valid JSON, no explanation:
{{"category": "...", "key": "...", "value": "..."}}

JSON:"""

    try:
        response = llm.generate(prompt=prompt)
        if response.error:
            logger.warning("LLM error extracting fact: %s", response.error)
            return None

        # Parse JSON
        import json
        text_resp = response.text.strip()
        # Try to find JSON object
        start = text_resp.find("{")
        end = text_resp.rfind("}") + 1
        if start >= 0 and end > start:
            fact = json.loads(text_resp[start:end])
            if all(k in fact for k in ["category", "key", "value"]):
                memory.store_fact(
                    category=fact["category"],
                    key=fact["key"],
                    value=str(fact["value"]),
                    confidence=0.95,  # High confidence - user explicitly stated
                    source="stated",
                )
                logger.info("Saved stated fact: %s.%s = %s", fact["category"], fact["key"], fact["value"])
                return fact["value"]
    except Exception as e:
        logger.warning("Failed to extract/save fact: %s", e)

    return None


def _is_addressing_joi(text: str, names: Optional[List[str]] = None) -> bool:
    """Check if the message is addressing Joi directly."""
    if names is None:
        names = JOI_NAMES_DEFAULT

    # Use cached regex if available
    names_key = tuple(sorted(names))
    if names_key not in _address_regex_cache:
        _address_regex_cache[names_key] = _build_address_regex(names)

    return bool(_address_regex_cache[names_key].search(text))


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
    group_names: Optional[List[str]] = None  # Names Joi responds to in this group


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

    # Handle reactions - store and respond briefly
    if msg.content.type == "reaction":
        emoji = msg.content.reaction or "?"
        logger.info("Received reaction %s from %s", emoji, msg.sender.transport_id)

        reaction_text = f"[reacted with {emoji}]"
        memory.store_message(
            message_id=msg.message_id,
            direction="inbound",
            content_type="reaction",
            content_text=reaction_text,
            timestamp=msg.timestamp,
            conversation_id=msg.conversation.id,
            sender_id=msg.sender.transport_id,
            sender_name=msg.sender.display_name,
        )

        # Generate brief reaction response (skip for store_only)
        if not msg.store_only:
            response_text = _generate_reaction_response(emoji, msg.conversation.id)
            if response_text:
                _send_to_mesh(
                    recipient_id=msg.sender.id,
                    recipient_transport_id=msg.sender.transport_id,
                    conversation=msg.conversation,
                    text=response_text,
                    reply_to=None,
                )
                # Store outbound
                memory.store_message(
                    message_id=str(uuid.uuid4()),
                    direction="outbound",
                    content_type="text",
                    content_text=response_text,
                    timestamp=int(time.time() * 1000),
                    conversation_id=msg.conversation.id,
                )

        return InboundResponse(status="ok", message_id=msg.message_id)

    # Only handle text messages beyond this point
    if msg.content.type != "text" or not msg.content.text:
        logger.info("Skipping unsupported message type=%s", msg.content.type)
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

    # Check for "remember this" requests (only from allowed senders)
    saved_fact = None
    if not msg.store_only:
        remember_what = _detect_remember_request(user_text)
        if remember_what:
            logger.info("Detected remember request: %s", remember_what[:50])
            saved_fact = _extract_and_save_fact(user_text, remember_what)

    # Determine if we should respond
    should_respond = True

    if msg.store_only:
        # Non-allowed sender in group - store only, no response
        logger.info("Message stored for context only (store_only=True)")
        should_respond = False
    elif msg.conversation.type == "group":
        # Group message from allowed sender - only respond if Joi is addressed
        # Use group-specific names if provided, otherwise fall back to default
        names_to_check = msg.group_names if msg.group_names else None
        if _is_addressing_joi(user_text, names=names_to_check):
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

    # Add hint if we just saved a fact
    if saved_fact:
        enriched_prompt += f"\n\n[You just saved this to memory: \"{saved_fact}\". Briefly acknowledge you'll remember it.]"

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


def _generate_reaction_response(emoji: str, conversation_id: str) -> Optional[str]:
    """Generate a brief response to a reaction based on context."""
    # Get recent context to understand what was reacted to
    recent = memory.get_recent_messages(limit=5, conversation_id=conversation_id)
    if not recent:
        return None

    # Find the last Joi message that was likely reacted to
    last_joi_msg = None
    for msg in reversed(recent):
        if msg.direction == "outbound" and msg.content_text:
            last_joi_msg = msg.content_text[:100]
            break

    if not last_joi_msg:
        return None

    # Generate brief contextual response
    prompt = f"""The user reacted to your message with {emoji}.
Your message was: "{last_joi_msg}"

Respond very briefly (1-5 words) acknowledging the reaction in a natural way.
Just the response, no explanation."""

    response = llm.generate(prompt=prompt)
    if response.error or not response.text:
        return None

    text = response.text.strip()
    # Keep it short
    if len(text) > 50:
        return None
    return text


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
