"""
Memory consolidation - extract facts and summarize old conversations.

This module handles:
1. Extracting user facts from conversations
2. Summarizing old messages into context_summaries
3. Cleaning up old messages after summarization

Prompts are configurable via files in /var/lib/joi/prompts/:
- default.fact_prompt, default.summary_prompt
- users/<id>.fact_prompt, users/<id>.summary_prompt
- groups/<id>.fact_prompt, groups/<id>.summary_prompt
"""

import json
import logging
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from .store import MemoryStore, Message

# Import prompt lookup functions
# Add parent directory to path for config import
sys.path.insert(0, __file__.rsplit("/", 2)[0])
from config import (
    get_fact_extraction_prompt_for_conversation,
    get_summarization_prompt_for_conversation,
    get_context_for_conversation_by_id,
    get_compact_window_for_conversation,
)

logger = logging.getLogger("joi.memory.consolidation")


def format_messages_for_llm(messages: List[Message]) -> str:
    """Format messages as conversation text for LLM."""
    lines = []
    for msg in messages:
        if msg.direction == "outbound":
            role = "Joi"
        else:
            # Use actual name/ID so LLM can attribute facts correctly
            role = msg.sender_name or msg.sender_id or "Someone"
        text = msg.content_text or "(no text)"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def parse_facts_json(response: str) -> List[Dict[str, Any]]:
    """Parse LLM response as JSON array of facts."""
    # Try to find JSON array in response
    response = response.strip()

    # If response starts with [ and ends with ], try to parse directly
    if response.startswith("["):
        try:
            parsed = json.loads(response)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Try to find JSON array in response
    match = re.search(r'\[[\s\S]*\]', response)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    clean = response.replace("\r", "").replace("\n", " ")[:30] if response else "(empty)"
    logger.warning("Could not parse facts from LLM response", extra={"preview": clean})
    return []


def validate_fact(fact: Any) -> bool:
    """Validate a fact dict has required fields and safe content."""
    if not isinstance(fact, dict):
        return False

    required = ["category", "key", "value"]
    if not all(k in fact for k in required):
        return False

    # Ensure values are the right types
    if not isinstance(fact.get("category"), str):
        return False
    if not isinstance(fact.get("key"), str):
        return False
    if fact.get("value") is None:
        return False

    # Category is free-form - just ensure it's a non-empty string
    if not fact.get("category"):
        return False

    # Check for injection patterns in value and key (defense against indirect prompt injection)
    suspicious_patterns = [
        r'ignore previous',
        r'disregard all',
        r'you are now',
        r'new instructions',
        r'SYSTEM PROMPT',
        r'CRITICAL INSTRUCTIONS',
        r'ignore all',
        r'forget everything',
    ]

    value_str = str(fact.get("value", ""))
    key_str = str(fact.get("key", ""))
    content_to_check = f"{key_str} {value_str}"

    for pattern in suspicious_patterns:
        if re.search(pattern, content_to_check, re.IGNORECASE):
            logger.warning("Fact rejected: injection pattern detected",
                extra={"pattern": pattern, "action": "fact_rejected"})
            return False

    # Confidence should be 0-1
    confidence = fact.get("confidence", 0.8)
    try:
        confidence = float(confidence)
        if confidence < 0 or confidence > 1:
            fact["confidence"] = 0.8
        else:
            fact["confidence"] = confidence
    except (TypeError, ValueError):
        fact["confidence"] = 0.8

    # ttl_hours must be a positive number if present
    if "ttl_hours" in fact:
        try:
            ttl = float(fact["ttl_hours"])
            if ttl <= 0:
                del fact["ttl_hours"]
            else:
                fact["ttl_hours"] = ttl
        except (TypeError, ValueError):
            del fact["ttl_hours"]

    return True


def validate_summary(summary: str) -> tuple[bool, str]:
    """
    Validate summary text before storing.
    Returns (is_valid, cleaned_summary).
    """
    if not summary or len(summary) < 10:
        return False, ""

    if len(summary) > 2000:
        summary = summary[:2000]

    # Check for suspicious patterns (injection attempts)
    suspicious_patterns = [
        r'ignore previous',
        r'disregard all',
        r'you are now',
        r'new instructions',
        r'SYSTEM PROMPT',
        r'CRITICAL INSTRUCTIONS',
    ]

    for pattern in suspicious_patterns:
        if re.search(pattern, summary, re.IGNORECASE):
            logger.warning("Suspicious pattern in summary", extra={"pattern": pattern})
            return False, ""

    return True, summary.strip()


ModelLookupFunc = Callable[[str], Optional[str]]


def _redact_convo_id(convo_id: str) -> str:
    """Redact conversation ID for privacy mode logging."""
    if not convo_id:
        return convo_id
    # Phone number pattern
    if convo_id.startswith("+") and len(convo_id) > 5:
        return f"+***{convo_id[-4:]}"
    # Group ID (long base64-like string)
    if len(convo_id) > 20:
        return f"[GRP:{convo_id[:4]}...]"
    return convo_id


class MemoryConsolidator:
    """Handles memory consolidation tasks."""

    def __init__(
        self,
        memory: MemoryStore,
        llm_client: Any,
        consolidation_model: Optional[str] = None,
        model_lookup: Optional[ModelLookupFunc] = None,
        privacy_mode: bool = False,
    ):
        """
        Initialize consolidator.

        Args:
            memory: MemoryStore instance
            llm_client: LLM client with generate() method
            consolidation_model: Default model name for consolidation tasks
                                (uses low temperature for precise extraction)
            model_lookup: Optional function to look up per-conversation model.
                         Takes conversation_id, returns model name or None.
                         Falls back to consolidation_model if returns None.
            privacy_mode: If True or callable returning True, redact conversation IDs in logs
        """
        self.memory = memory
        self.llm = llm_client
        self.consolidation_model = consolidation_model
        self.model_lookup = model_lookup
        self._privacy_mode = privacy_mode

    def _is_privacy_mode(self) -> bool:
        """Check if privacy mode is enabled (supports callable or bool)."""
        if callable(self._privacy_mode):
            return self._privacy_mode()
        return bool(self._privacy_mode)

    def _log_convo_id(self, convo_id: str) -> str:
        """Return conversation ID for logging, redacted if privacy mode."""
        return _redact_convo_id(convo_id) if self._is_privacy_mode() else convo_id

    def _get_model_for_conversation(self, conversation_id: str) -> Optional[str]:
        """Get the consolidation model for a conversation."""
        if self.model_lookup:
            model = self.model_lookup(conversation_id)
            if model:
                return model
        return self.consolidation_model

    def extract_facts_from_messages(
        self,
        messages: List[Message],
        store: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Extract facts from a list of messages using LLM.

        Args:
            messages: Messages to analyze
            store: Whether to store extracted facts

        Returns:
            List of extracted fact dicts
        """
        if not messages:
            return []

        # Only extract facts from what the user said — Joi's outbound messages
        # (including [JOI-REMINDER] and [JOI-WIND] proactives) contain no user facts.
        inbound_messages = [m for m in messages if m.direction == "inbound"]
        # detected_at = timestamp of the most recent inbound message in the batch
        detected_at = max((m.timestamp for m in inbound_messages), default=None) if inbound_messages else None
        if not inbound_messages:
            return []

        # Get conversation ID, model and prompt for this conversation
        convo_id = messages[0].conversation_id if messages else ""
        model = self._get_model_for_conversation(convo_id)
        prompt_template = get_fact_extraction_prompt_for_conversation(convo_id)

        conversation_text = format_messages_for_llm(inbound_messages)
        prompt = prompt_template.format(conversation=conversation_text)

        try:
            response = self.llm.generate(prompt=prompt, model=model)
        except Exception as e:
            logger.error("LLM generate failed", extra={"error": str(e)})
            return []

        if response.error:
            logger.error("LLM error during fact extraction", extra={"error": response.error})
            return []

        logger.debug("LLM response for facts", extra={
            "length": len(response.text) if response.text else 0,
            "preview": response.text[:300] if response.text else "(empty)"
        })

        try:
            facts = parse_facts_json(response.text)
        except Exception as e:
            logger.error("parse_facts_json failed", extra={"error": str(e)})
            return []

        # Log what happened at INFO level for visibility
        if facts:
            logger.info("Fact extraction found facts", extra={"count": len(facts)})
        elif response.text and response.text.strip() == "[]":
            logger.info("Fact extraction: LLM returned empty array (no facts found in conversation)")
        else:
            # Single line preview: strip newlines first, then truncate
            clean = response.text.replace("\r", "").replace("\n", " ").strip() if response.text else ""
            preview = (clean[:30] + "...") if len(clean) > 30 else (clean or "(empty)")
            logger.info("Fact extraction: LLM response didn't parse", extra={"preview": preview})

        # Retry once if parsing failed but we got a response
        if not facts and response.text and len(response.text.strip()) > 10:
            logger.info("Fact extraction retry: first response was not JSON, asking again")
            retry_prompt = f"""Your previous response was not valid JSON.

Return ONLY a JSON array, nothing else. No explanation, no markdown, no bullet points.

If you found facts, format them like this:
[{{"category": "personal", "key": "name", "value": "John", "confidence": 0.9}}]

If no facts, return exactly: []

Previous response that failed:
{response.text[:500]}

Corrected JSON:"""
            try:
                retry_response = self.llm.generate(prompt=retry_prompt, model=model)
                if retry_response.text and not retry_response.error:
                    facts = parse_facts_json(retry_response.text)
                    if facts:
                        logger.info("Fact extraction retry succeeded", extra={"count": len(facts)})
            except Exception as e:
                logger.warning("Fact extraction retry failed", extra={"error": str(e)})

        valid_facts = []
        for f in facts:
            try:
                if validate_fact(f):
                    valid_facts.append(f)
            except Exception as e:
                logger.warning("validate_fact error", extra={"fact": str(f), "error": str(e)})

        if store and valid_facts:
            # Store facts under conversation_id (works for both DMs and groups)

            # Detect if this is a group (not a phone number)
            is_group = convo_id and not convo_id.startswith("+")

            stored_count = 0
            important_count = 0
            for fact in valid_facts:
                try:
                    fact_key = fact["key"]
                    # Mood is tracked per-message in WindState, not as a fact
                    if fact_key == "current_mood":
                        continue
                    fact_value = str(fact["value"])

                    # For groups, try to extract name from value and prefix key
                    if is_group and fact_value:
                        # Extract first word as likely name (e.g., "Peter is a developer" -> "peter")
                        first_word = fact_value.split()[0].lower() if fact_value.split() else ""
                        # Only use if it looks like a name (capitalized in original, reasonable length)
                        original_first = fact_value.split()[0] if fact_value.split() else ""
                        if original_first and original_first[0].isupper() and 2 <= len(first_word) <= 20:
                            fact_key = f"{first_word}_{fact['key']}"

                    # Check if fact is marked as core (important)
                    is_important = bool(fact.get("core"))
                    if is_important:
                        important_count += 1

                    self.memory.store_fact(
                        category=fact["category"],
                        key=fact_key,
                        value=fact_value,
                        confidence=float(fact.get("confidence", 0.8)),
                        source="inferred",
                        conversation_id=convo_id,
                        important=bool(is_important),
                        ttl_hours=fact.get("ttl_hours"),
                        detected_at=detected_at,
                    )
                    stored_count += 1
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning("Skipping malformed fact", extra={"fact": str(fact), "error": str(e)})
            if stored_count:
                logger.info("Extracted and stored facts", extra={
                    "count": stored_count,
                    "core_count": important_count if important_count else None,
                    "conversation_id": self._log_convo_id(convo_id)
                })

        return valid_facts

    def summarize_messages(
        self,
        messages: List[Message],
        store: bool = True,
    ) -> Optional[str]:
        """
        Summarize a list of messages using LLM.

        Args:
            messages: Messages to summarize
            store: Whether to store the summary

        Returns:
            Summary text or None if failed
        """
        if not messages:
            return None

        # Strip proactive outbound messages ([JOI-REMINDER], [JOI-WIND]) — they are not
        # conversation and cause the LLM to confabulate dialogue around them.
        # Normal Joi replies are kept.
        _PROACTIVE = ("[JOI-REMINDER]", "[JOI-WIND]")
        messages = [
            m for m in messages
            if not (m.direction == "outbound"
                    and m.content_text
                    and m.content_text.startswith(_PROACTIVE))
        ]
        if not messages:
            return None

        # Get conversation ID, model and prompt for this conversation
        convo_id = messages[0].conversation_id if messages else ""
        model = self._get_model_for_conversation(convo_id)
        prompt_template = get_summarization_prompt_for_conversation(convo_id)

        conversation_text = format_messages_for_llm(messages)
        prompt = prompt_template.format(conversation=conversation_text)

        response = self.llm.generate(prompt=prompt, model=model)
        if response.error:
            logger.error("LLM error during summarization", extra={"error": response.error})
            return None

        is_valid, summary = validate_summary(response.text)
        if not is_valid:
            logger.warning("Summary validation failed")
            logger.debug("Summary validation failed, raw response: %s", response.text[:200] if response.text else "(empty)")
            return None

        if store:
            period_start = min(m.timestamp for m in messages)
            period_end = max(m.timestamp for m in messages)

            self.memory.store_summary(
                summary_type="conversation",
                period_start=period_start,
                period_end=period_end,
                summary_text=summary,
                message_count=len(messages),
                conversation_id=convo_id or "",
            )

        return summary

    def run_consolidation(
        self,
        context_messages: int = 50,
        compact_batch_size: int = 20,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run count-based memory consolidation per conversation.

        Triggers per conversation when message_count > context_messages.
        Compacts the oldest `compact_batch_size` messages.

        This approach ensures messages are compacted while still fresh
        (just exited context window), avoiding "memory drift" where
        messages are forgotten then suddenly remembered via summary.

        Compacted messages are always archived (archived=1). Hard deletion
        is handled separately by the time-based purge (JOI_MESSAGE_RETENTION_DAYS).

        Args:
            context_messages: Context window size (trigger when exceeded)
            compact_batch_size: Number of oldest messages to compact
            conversation_id: If provided, only consolidate this conversation (skip full scan)

        Returns:
            Dict with consolidation results (totals across all conversations)
        """
        results = {
            "ran": False,
            "reason": None,
            "facts_extracted": 0,
            "messages_summarized": 0,
            "messages_archived": 0,
            "conversations_processed": 0,
        }

        # Use provided conversation_id or scan all conversations
        if conversation_id:
            conversation_ids = [conversation_id]
        else:
            conversation_ids = self.memory.get_distinct_conversation_ids(min_messages=1)

        for convo_id in conversation_ids:
            convo_results = self._consolidate_conversation(
                conversation_id=convo_id,
                context_messages=context_messages,
                compact_batch_size=compact_batch_size,
            )

            if convo_results["ran"]:
                results["ran"] = True
                results["reason"] = "context_overflow"
                results["facts_extracted"] += convo_results["facts_extracted"]
                results["messages_summarized"] += convo_results["messages_summarized"]
                results["messages_archived"] += convo_results["messages_archived"]
                results["conversations_processed"] += 1

        if results["ran"]:
            logger.info("Consolidation complete", extra={
                "facts_extracted": results["facts_extracted"],
                "messages_summarized": results["messages_summarized"],
                "messages_archived": results["messages_archived"],
                "conversations_processed": results["conversations_processed"]
            })
        return results

    def _consolidate_conversation(
        self,
        conversation_id: str,
        context_messages: int,
        compact_batch_size: int,
        compact_all: bool = False,
    ) -> Dict[str, Any]:
        """
        Consolidate a single conversation using count-based trigger.

        Compacts oldest messages when total exceeds context window.
        Per-conversation settings (.context, .compact_window files) override defaults.

        Args:
            conversation_id: The conversation to consolidate
            context_messages: Context window size (trigger when exceeded)
            compact_batch_size: Number of oldest messages to compact
            compact_all: If True, compact ALL messages beyond context_messages
                        (batch_size = msg_count - context_messages).
                        If False, use compact_batch_size (original behavior).
        """
        results = {
            "ran": False,
            "reason": None,
            "facts_extracted": 0,
            "messages_summarized": 0,
            "messages_archived": 0,
        }

        # Look up per-conversation settings (override defaults if configured)
        custom_context = get_context_for_conversation_by_id(conversation_id)
        if custom_context is not None:
            context_messages = custom_context

        custom_compact = get_compact_window_for_conversation(conversation_id)
        if custom_compact is not None:
            compact_batch_size = custom_compact

        # Check message count for this conversation
        message_count = self.memory.get_message_count_for_conversation(conversation_id)

        # If compact_all mode, compact ALL messages (for Wind fresh start)
        if compact_all:
            if message_count <= 0:
                return results
            compact_batch_size = message_count
        elif message_count <= context_messages:
            # Normal mode: only trigger when context window exceeded
            return results

        results["ran"] = True
        results["reason"] = "context_overflow"

        # Get the oldest `compact_batch_size` messages for compaction
        oldest_messages = self.memory.get_oldest_messages(
            limit=compact_batch_size,
            conversation_id=conversation_id,
        )

        if not oldest_messages:
            return results

        logger.info("Compacting oldest messages", extra={
            "count": len(oldest_messages),
            "conversation_id": self._log_convo_id(conversation_id),
            "total_messages": message_count,
            "context_size": context_messages
        })

        # Extract facts (with error handling)
        try:
            facts = self.extract_facts_from_messages(oldest_messages, store=True)
            results["facts_extracted"] = len(facts)
        except Exception as e:
            logger.error("Fact extraction failed", extra={
                "conversation_id": self._log_convo_id(conversation_id),
                "error": str(e)
            }, exc_info=True)
            results["facts_extracted"] = 0

        # Summarize (with error handling)
        try:
            summary = self.summarize_messages(oldest_messages, store=True)
        except Exception as e:
            logger.error("Summarization failed", extra={
                "conversation_id": self._log_convo_id(conversation_id),
                "error": str(e)
            }, exc_info=True)
            summary = None

        # Fail-safe: only remove messages if summarization succeeded
        # If summary fails, messages remain and will be retried next consolidation run
        # (may cause duplicate fact extraction, but prevents data loss)
        if summary:
            results["messages_summarized"] = len(oldest_messages)

            # Remove compacted messages by ID (not timestamp, to avoid boundary issues)
            message_ids = [m.message_id for m in oldest_messages]
            removed = self.memory.archive_messages_by_ids(message_ids, conversation_id)
            results["messages_archived"] = removed

        return results
