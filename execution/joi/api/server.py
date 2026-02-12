import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# Add api/ and parent dirs to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # api/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # joi/

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hmac_auth import (
    NonceStore,
    create_request_headers,
    get_shared_secret,
    verify_hmac,
    verify_timestamp,
    DEFAULT_TIMESTAMP_TOLERANCE_MS,
)

from config import load_settings, get_prompt_for_conversation, ensure_prompts_dir
from llm import OllamaClient
from memory import MemoryConsolidator, MemoryStore

logger = logging.getLogger("joi.api")


# --- Priority Message Queue ---

@dataclass(order=True)
class PrioritizedMessage:
    """Message wrapper for priority queue. Lower priority number = higher priority."""
    priority: int
    timestamp: float = field(compare=False)
    message_id: str = field(compare=False)
    handler: Callable = field(compare=False)
    result: Any = field(default=None, compare=False)
    error: Optional[str] = field(default=None, compare=False)
    done_event: threading.Event = field(default_factory=threading.Event, compare=False)


class MessageQueue:
    """Global message queue with priority support and single worker."""

    PRIORITY_OWNER = 0  # Owner messages processed first
    PRIORITY_NORMAL = 1  # Other allowed senders

    def __init__(self):
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._current_message_id: Optional[str] = None

    def start(self):
        """Start the worker thread."""
        if self._worker_thread is not None:
            return
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Message queue worker started")

    def stop(self):
        """Stop the worker thread."""
        self._running = False
        # Put a sentinel to unblock the queue
        self._queue.put(PrioritizedMessage(
            priority=999,
            timestamp=time.time(),
            message_id="__stop__",
            handler=lambda: None,
        ))
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None
        logger.info("Message queue worker stopped")

    def enqueue(self, message_id: str, handler: Callable, is_owner: bool = False, timeout: float = 300.0) -> Any:
        """
        Add message to queue and wait for processing.

        Args:
            message_id: Unique message identifier
            handler: Function to call for processing (returns result)
            is_owner: If True, gets priority processing
            timeout: Max seconds to wait for processing

        Returns:
            Result from handler

        Raises:
            TimeoutError: If processing takes too long
            Exception: If handler raises an error
        """
        priority = self.PRIORITY_OWNER if is_owner else self.PRIORITY_NORMAL
        msg = PrioritizedMessage(
            priority=priority,
            timestamp=time.time(),
            message_id=message_id,
            handler=handler,
        )

        queue_size = self._queue.qsize()
        priority_label = "owner" if priority == self.PRIORITY_OWNER else "normal"
        logger.info("Queue ADD: message_id=%s priority=%s queue_size=%d", message_id, priority_label, queue_size)

        self._queue.put(msg)

        # Wait for processing to complete
        if not msg.done_event.wait(timeout=timeout):
            raise TimeoutError(f"Message {message_id} processing timed out after {timeout}s")

        if msg.error:
            raise Exception(msg.error)

        return msg.result

    def _worker_loop(self):
        """Process messages from queue sequentially."""
        while self._running:
            try:
                msg = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if msg.message_id == "__stop__":
                break

            self._current_message_id = msg.message_id
            start_time = time.time()
            priority_label = "owner" if msg.priority == self.PRIORITY_OWNER else "normal"
            logger.info("Queue START: message_id=%s priority=%s", msg.message_id, priority_label)

            try:
                msg.result = msg.handler()
            except Exception as e:
                logger.error("Queue ERROR: message_id=%s error=%s", msg.message_id, e)
                msg.error = str(e)
            finally:
                elapsed = time.time() - start_time
                logger.info("Queue DONE: message_id=%s elapsed=%.2fs", msg.message_id, elapsed)
                self._current_message_id = None
                msg.done_event.set()

    def get_queue_size(self) -> int:
        """Get current queue size."""
        return self._queue.qsize()


# Global message queue instance
message_queue = MessageQueue()


settings = load_settings()

app = FastAPI(title="joi-api", version="0.1.0")

# HMAC authentication settings
HMAC_SECRET = get_shared_secret()
HMAC_ENABLED = HMAC_SECRET is not None
HMAC_TIMESTAMP_TOLERANCE_MS = int(os.getenv("JOI_HMAC_TIMESTAMP_TOLERANCE_MS", str(DEFAULT_TIMESTAMP_TOLERANCE_MS)))

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

# Initialize nonce store for replay protection (separate connection to same DB)
nonce_store: Optional[NonceStore] = None
if HMAC_ENABLED:
    nonce_db_path = os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db")
    nonce_store = NonceStore(nonce_db_path)

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
# Default name for @mention detection in groups (can be overridden per-group via mesh)
JOI_NAME_DEFAULT = ["Joi"]


def _build_address_regex(names: list) -> re.Pattern:
    """Build regex pattern for addressing detection from list of names.

    Only matches explicit @Name mentions (Signal group mention style).
    """
    patterns = []
    for name in names:
        escaped = re.escape(name)
        patterns.extend([
            rf"^@{escaped}(?:\s|$|[,:.!?])",   # "@Name" at start
            rf"\s@{escaped}(?:\s|$|[,:.!?])",  # "@Name" in middle/end
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


def _extract_and_save_fact(text: str, remember_what: str, conversation_id: str = "") -> Optional[str]:
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
                    conversation_id=conversation_id,
                )
                logger.info("Saved stated fact for %s: %s.%s = %s", conversation_id or "global", fact["category"], fact["key"], fact["value"])
                return fact["value"]
    except Exception as e:
        logger.warning("Failed to extract/save fact: %s", e)

    return None


def _is_addressing_joi(text: str, names: Optional[List[str]] = None) -> bool:
    """Check if the message is addressing Joi directly via @mention."""
    if names is None:
        names = JOI_NAME_DEFAULT

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
    bot_mentioned: bool = False  # True if bot was @mentioned via Signal mention


class InboundResponse(BaseModel):
    status: str
    message_id: Optional[str] = None
    error: Optional[str] = None


# --- HMAC Middleware ---

@app.middleware("http")
async def hmac_verification_middleware(request: Request, call_next):
    """Verify HMAC authentication for mesh → joi requests."""
    # Skip HMAC for health endpoint (monitoring)
    if request.url.path == "/health":
        return await call_next(request)

    # Skip if HMAC not configured
    if not HMAC_ENABLED:
        return await call_next(request)

    # Extract headers
    nonce = request.headers.get("X-Nonce")
    timestamp_str = request.headers.get("X-Timestamp")
    signature = request.headers.get("X-HMAC-SHA256")

    # Check all required headers present
    if not all([nonce, timestamp_str, signature]):
        logger.warning("HMAC auth failed: missing headers")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_missing_headers", "message": "Missing authentication headers"}}
        )

    # Parse timestamp
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        logger.warning("HMAC auth failed: invalid timestamp format")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_invalid_timestamp", "message": "Invalid timestamp format"}}
        )

    # Verify timestamp freshness
    ts_valid, ts_error = verify_timestamp(timestamp, HMAC_TIMESTAMP_TOLERANCE_MS)
    if not ts_valid:
        logger.warning("HMAC auth failed: %s", ts_error)
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": ts_error, "message": "Request timestamp out of tolerance"}}
        )

    # Verify nonce not replayed
    nonce_valid, nonce_error = nonce_store.check_and_store(nonce, source="mesh")
    if not nonce_valid:
        logger.warning("HMAC auth failed: %s nonce=%s", nonce_error, nonce[:8])
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": nonce_error, "message": "Nonce already used"}}
        )

    # Read body for HMAC verification
    body = await request.body()

    # Verify HMAC signature
    if not verify_hmac(nonce, timestamp, body, signature, HMAC_SECRET):
        logger.warning("HMAC auth failed: invalid signature")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_invalid_signature", "message": "Invalid HMAC signature"}}
        )

    logger.debug("HMAC auth passed for %s", request.url.path)
    return await call_next(request)


# --- Lifecycle Events ---

@app.on_event("startup")
def startup_event():
    """Start the message queue worker on app startup."""
    message_queue.start()
    if HMAC_ENABLED:
        logger.info("Joi API started with message queue (HMAC enabled)")
    else:
        logger.warning("Joi API started with message queue (HMAC DISABLED - set JOI_HMAC_SECRET)")


@app.on_event("shutdown")
def shutdown_event():
    """Stop the message queue worker on app shutdown."""
    message_queue.stop()
    logger.info("Joi API shutting down")


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
        },
        "queue": {
            "size": message_queue.get_queue_size(),
        }
    }


@app.post("/api/v1/message/inbound", response_model=InboundResponse)
def receive_message(msg: InboundMessage):
    """
    Receive a message from mesh proxy, process with LLM, send response back.

    Messages requiring LLM are queued and processed sequentially.
    Owner messages get priority in the queue.

    For group messages:
    - store_only=True: Store for context but don't respond (non-allowed senders)
    - store_only=False: Check if Joi is addressed before responding
    """
    # Check if sender is owner (id="owner" is set by mesh for allowed senders)
    is_owner = msg.sender.id == "owner"

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
            saved_fact = _extract_and_save_fact(user_text, remember_what, conversation_id=msg.conversation.id)

    # Determine if we should respond
    should_respond = True

    if msg.store_only:
        # Non-allowed sender in group - store only, no response
        logger.info("Message stored for context only (store_only=True)")
        should_respond = False
    elif msg.conversation.type == "group":
        # Group message from allowed sender - only respond if Joi is addressed
        # Check Signal @mention (bot_mentioned) or text-based @name
        if msg.bot_mentioned:
            logger.info("Joi @mentioned in group message (Signal mention), will respond")
            should_respond = True
        else:
            # Fallback: check text for @name pattern
            names_to_check = msg.group_names if msg.group_names else None
            if _is_addressing_joi(user_text, names=names_to_check):
                logger.info("Joi addressed in group message (text pattern), will respond")
                should_respond = True
            else:
                logger.info("Joi not addressed in group message, storing only")
                should_respond = False

    if not should_respond:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # --- Queue the LLM processing ---
    # This ensures messages are processed sequentially with owner priority

    def process_with_llm() -> InboundResponse:
        """Process message with LLM - runs in queue worker thread."""
        # Build conversation context from recent messages
        recent_messages = memory.get_recent_messages(
            limit=CONTEXT_MESSAGE_COUNT,
            conversation_id=msg.conversation.id,
        )

        # Convert to LLM chat format (with sender prefix for groups)
        is_group_chat = msg.conversation.type == "group"
        chat_messages = _build_chat_messages(recent_messages, is_group=is_group_chat)

        # Get per-conversation system prompt and enrich it
        base_prompt = get_prompt_for_conversation(
            conversation_type=msg.conversation.type,
            conversation_id=msg.conversation.id,
            sender_id=msg.sender.transport_id,
        )
        enriched_prompt = _build_enriched_prompt(base_prompt, user_text, conversation_id=msg.conversation.id)

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

        # Check if memory consolidation needed
        _maybe_run_consolidation()

        return InboundResponse(status="ok", message_id=msg.message_id)

    # Enqueue and wait for processing (owner gets priority)
    try:
        result = message_queue.enqueue(
            message_id=msg.message_id,
            handler=process_with_llm,
            is_owner=is_owner,
            timeout=LLM_TIMEOUT + 30,  # LLM timeout + buffer
        )
        return result
    except TimeoutError:
        logger.error("Message %s timed out in queue", msg.message_id)
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error="queue_timeout",
        )
    except Exception as e:
        logger.error("Message %s queue error: %s", msg.message_id, e)
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error=str(e),
        )


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


def _build_enriched_prompt(base_prompt: str, user_message: Optional[str] = None, conversation_id: Optional[str] = None) -> str:
    """Build system prompt enriched with user facts, summaries, and RAG context for this conversation."""
    parts = [base_prompt]

    # Add user facts for this conversation
    facts_text = memory.get_facts_as_text(min_confidence=0.6, conversation_id=conversation_id)
    if facts_text:
        parts.append("\n\n" + facts_text)

    # Add recent conversation summaries for this conversation
    summaries_text = memory.get_summaries_as_text(days=7, conversation_id=conversation_id)
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
            context_messages=CONTEXT_MESSAGE_COUNT,
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
    import json

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
        # Serialize to bytes for HMAC
        body = json.dumps(payload).encode("utf-8")

        # Build headers with HMAC if configured
        headers = {"Content-Type": "application/json"}
        if HMAC_ENABLED:
            hmac_headers = create_request_headers(body, HMAC_SECRET)
            headers.update(hmac_headers)

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, content=body, headers=headers)
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
