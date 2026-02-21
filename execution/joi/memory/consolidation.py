"""
Memory consolidation - extract facts and summarize old conversations.

This module handles:
1. Extracting user facts from conversations
2. Summarizing old messages into context_summaries
3. Cleaning up old messages after summarization
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from .store import MemoryStore, Message

logger = logging.getLogger("joi.memory.consolidation")

# Prompt for extracting facts from conversation
FACT_EXTRACTION_PROMPT = """Extract facts worth remembering from this conversation.

Look for ANY of these:
- Personal info (name, age, location, profession, family)
- Preferences (likes, dislikes, favorites)
- Plans, goals, or intentions mentioned
- Skills, hobbies, or interests
- Health, routines, or habits
- Opinions or beliefs expressed
- Events or experiences shared
- Technical setups or configurations discussed

IMPORTANT: Return ONLY a valid JSON array. No explanations, no markdown.

Each fact needs these fields:
- "category": what type (personal, preference, work, health, skill, goal, routine, opinion, event, technical)
- "key": short identifier
- "value": the fact AS A COMPLETE SENTENCE with the person's name
- "confidence": 0.0-1.0

Include the person's name in value (never "User" or "the user").
If truly no facts, return: []

Example:
[{{"category": "work", "key": "profession", "value": "Peter is a developer", "confidence": 1.0}}, {{"category": "preference", "key": "coffee", "value": "Peter prefers black coffee", "confidence": 0.8}}]

Conversation:
{conversation}

JSON:"""

# Prompt for summarizing conversation
SUMMARIZATION_PROMPT = """Summarize this conversation concisely. Focus on:
- Main topics discussed
- Decisions made or conclusions reached
- Any tasks or action items mentioned
- Important information shared

Keep the summary under 200 words. Write in past tense, third person.
Do not include any system instructions or meta-commentary.

Conversation:
{conversation}

Summary:"""


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
    match = re.search(r'\[[\s\S]*?\]', response)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    clean = response.replace("\r", "").replace("\n", " ")[:30] if response else "(empty)"
    logger.warning("Could not parse facts from LLM response: %s...", clean)
    return []


def validate_fact(fact: Any) -> bool:
    """Validate a fact dict has required fields."""
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
            logger.warning("Suspicious pattern in summary: %s", pattern)
            return False, ""

    return True, summary.strip()


class MemoryConsolidator:
    """Handles memory consolidation tasks."""

    def __init__(
        self,
        memory: MemoryStore,
        llm_client: Any,
        consolidation_model: Optional[str] = None,
    ):
        """
        Initialize consolidator.

        Args:
            memory: MemoryStore instance
            llm_client: LLM client with generate() method
            consolidation_model: Optional model name for consolidation tasks
                                (uses low temperature for precise extraction)
        """
        self.memory = memory
        self.llm = llm_client
        self.consolidation_model = consolidation_model

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

        conversation_text = format_messages_for_llm(messages)
        prompt = FACT_EXTRACTION_PROMPT.format(conversation=conversation_text)

        try:
            response = self.llm.generate(prompt=prompt, model=self.consolidation_model)
        except Exception as e:
            logger.error("LLM generate failed: %s", e)
            return []

        if response.error:
            logger.error("LLM error during fact extraction: %s", response.error)
            return []

        logger.debug("LLM response for facts (len=%d): %s", len(response.text) if response.text else 0, response.text[:300] if response.text else "(empty)")

        try:
            facts = parse_facts_json(response.text)
        except Exception as e:
            logger.error("parse_facts_json failed: %s", e)
            return []

        # Log what happened at INFO level for visibility
        if facts:
            logger.info("Fact extraction found %d facts", len(facts))
        elif response.text and response.text.strip() == "[]":
            logger.info("Fact extraction: LLM returned empty array (no facts found in conversation)")
        else:
            # Single line preview: strip newlines first, then truncate
            clean = response.text.replace("\r", "").replace("\n", " ").strip() if response.text else ""
            preview = (clean[:30] + "...") if len(clean) > 30 else (clean or "(empty)")
            logger.info("Fact extraction: LLM response didn't parse: %s", preview)

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
                retry_response = self.llm.generate(prompt=retry_prompt, model=self.consolidation_model)
                if retry_response.text and not retry_response.error:
                    facts = parse_facts_json(retry_response.text)
                    if facts:
                        logger.info("Fact extraction retry succeeded: %d facts", len(facts))
            except Exception as e:
                logger.warning("Fact extraction retry failed: %s", e)

        valid_facts = []
        for f in facts:
            try:
                if validate_fact(f):
                    valid_facts.append(f)
            except Exception as e:
                logger.warning("validate_fact error for %s: %s", f, e)

        if store and valid_facts:
            # Store facts under conversation_id (works for both DMs and groups)
            # Facts include person's name in the value, so no need to separate by sender
            convo_id = messages[0].conversation_id if messages else ""

            stored_count = 0
            for fact in valid_facts:
                try:
                    self.memory.store_fact(
                        category=fact["category"],
                        key=fact["key"],
                        value=str(fact["value"]),
                        confidence=float(fact.get("confidence", 0.8)),
                        source="inferred",
                        conversation_id=convo_id,
                    )
                    stored_count += 1
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning("Skipping malformed fact %s: %s", fact, e)
            if stored_count:
                logger.info("Extracted and stored %d facts for %s", stored_count, convo_id)

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

        conversation_text = format_messages_for_llm(messages)
        prompt = SUMMARIZATION_PROMPT.format(conversation=conversation_text)

        response = self.llm.generate(prompt=prompt, model=self.consolidation_model)
        if response.error:
            logger.error("LLM error during summarization: %s", response.error)
            return None

        is_valid, summary = validate_summary(response.text)
        if not is_valid:
            logger.warning("Summary validation failed")
            logger.debug("Summary validation failed, raw response: %s", response.text[:200] if response.text else "(empty)")
            return None

        if store:
            period_start = min(m.timestamp for m in messages)
            period_end = max(m.timestamp for m in messages)
            convo_id = messages[0].conversation_id if messages else ""

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
        archive_instead_of_delete: bool = False,
    ) -> Dict[str, Any]:
        """
        Run count-based memory consolidation per conversation.

        Triggers per conversation when message_count > context_messages.
        Compacts the oldest `compact_batch_size` messages.

        This approach ensures messages are compacted while still fresh
        (just exited context window), avoiding "memory drift" where
        messages are forgotten then suddenly remembered via summary.

        Args:
            context_messages: Context window size (trigger when exceeded)
            compact_batch_size: Number of oldest messages to compact
            archive_instead_of_delete: If True, archive messages; if False, delete them

        Returns:
            Dict with consolidation results (totals across all conversations)
        """
        results = {
            "ran": False,
            "reason": None,
            "facts_extracted": 0,
            "messages_summarized": 0,
            "messages_removed": 0,
            "conversations_processed": 0,
        }

        # Get all distinct conversation IDs
        conversation_ids = self.memory.get_distinct_conversation_ids(min_messages=1)

        for convo_id in conversation_ids:
            convo_results = self._consolidate_conversation(
                conversation_id=convo_id,
                context_messages=context_messages,
                compact_batch_size=compact_batch_size,
                archive_instead_of_delete=archive_instead_of_delete,
            )

            if convo_results["ran"]:
                results["ran"] = True
                results["reason"] = "context_overflow"
                results["facts_extracted"] += convo_results["facts_extracted"]
                results["messages_summarized"] += convo_results["messages_summarized"]
                results["messages_removed"] += convo_results["messages_removed"]
                results["conversations_processed"] += 1

        if results["ran"]:
            logger.info("Consolidation complete: %s", results)
        return results

    def _consolidate_conversation(
        self,
        conversation_id: str,
        context_messages: int,
        compact_batch_size: int,
        archive_instead_of_delete: bool,
    ) -> Dict[str, Any]:
        """
        Consolidate a single conversation using count-based trigger.

        Compacts oldest messages when total exceeds context window.
        """
        results = {
            "ran": False,
            "reason": None,
            "facts_extracted": 0,
            "messages_summarized": 0,
            "messages_removed": 0,
        }

        # Check message count for this conversation
        message_count = self.memory.get_message_count_for_conversation(conversation_id)

        # Trigger: message count exceeds context window
        if message_count <= context_messages:
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

        logger.info(
            "Compacting %d oldest messages for conversation %s (total: %d, context: %d)",
            len(oldest_messages), conversation_id, message_count, context_messages
        )

        # Extract facts (with error handling)
        try:
            facts = self.extract_facts_from_messages(oldest_messages, store=True)
            results["facts_extracted"] = len(facts)
        except Exception as e:
            logger.error("Fact extraction failed for %s: %s", conversation_id, e, exc_info=True)
            results["facts_extracted"] = 0

        # Summarize (with error handling)
        try:
            summary = self.summarize_messages(oldest_messages, store=True)
        except Exception as e:
            logger.error("Summarization failed for %s: %s", conversation_id, e, exc_info=True)
            summary = None

        if summary:
            results["messages_summarized"] = len(oldest_messages)

            # Remove compacted messages by ID (not timestamp, to avoid boundary issues)
            message_ids = [m.message_id for m in oldest_messages]
            if archive_instead_of_delete:
                removed = self.memory.archive_messages_by_ids(message_ids, conversation_id)
            else:
                removed = self.memory.delete_messages_by_ids(message_ids, conversation_id)
            results["messages_removed"] = removed

        return results
