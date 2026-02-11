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
FACT_EXTRACTION_PROMPT = """Analyze this conversation and extract any facts about the user.

Return a JSON array of facts. Each fact should have:
- category: one of "personal", "preference", "relationship", "work", "routine", "interest"
- key: short identifier (e.g., "name", "favorite_food", "partner_name")
- value: the fact itself
- confidence: 0.0-1.0 how confident you are (1.0 = user explicitly stated, 0.6 = inferred)

Only include facts that are clearly stated or strongly implied. Do not make assumptions.
If no facts can be extracted, return an empty array: []

Example output:
[
  {"category": "personal", "key": "name", "value": "Peter", "confidence": 1.0},
  {"category": "preference", "key": "coffee", "value": "prefers black coffee", "confidence": 0.8}
]

Conversation:
{conversation}

JSON array of extracted facts:"""

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
        role = "User" if msg.direction == "inbound" else "Joi"
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

    logger.warning("Could not parse facts from LLM response: %s", response[:200])
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

    valid_categories = ["personal", "preference", "relationship", "work", "routine", "interest"]
    if fact.get("category") not in valid_categories:
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

    def __init__(self, memory: MemoryStore, llm_client: Any):
        """
        Initialize consolidator.

        Args:
            memory: MemoryStore instance
            llm_client: LLM client with generate() method
        """
        self.memory = memory
        self.llm = llm_client

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

        response = self.llm.generate(prompt=prompt)
        if response.error:
            logger.error("LLM error during fact extraction: %s", response.error)
            return []

        facts = parse_facts_json(response.text)
        valid_facts = [f for f in facts if validate_fact(f)]

        if store and valid_facts:
            stored_count = 0
            for fact in valid_facts:
                try:
                    self.memory.store_fact(
                        category=fact["category"],
                        key=fact["key"],
                        value=str(fact["value"]),  # Ensure value is string
                        confidence=float(fact.get("confidence", 0.8)),
                        source="inferred",
                    )
                    stored_count += 1
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning("Skipping malformed fact %s: %s", fact, e)
            if stored_count:
                logger.info("Extracted and stored %d facts", stored_count)

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

        response = self.llm.generate(prompt=prompt)
        if response.error:
            logger.error("LLM error during summarization: %s", response.error)
            return None

        is_valid, summary = validate_summary(response.text)
        if not is_valid:
            logger.warning("Summary validation failed")
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
            )

        return summary

    def run_consolidation(
        self,
        silence_threshold_ms: int = 3600000,  # 1 hour
        max_messages_before_consolidation: int = 200,
        keep_recent_messages: int = 50,
        archive_instead_of_delete: bool = False,
    ) -> Dict[str, Any]:
        """
        Run full memory consolidation.

        Triggers when:
        - Silence for more than threshold, OR
        - More than max_messages in database

        Args:
            silence_threshold_ms: Consider conversation ended after this much silence
            max_messages_before_consolidation: Force consolidation at this count
            keep_recent_messages: Don't consolidate the most recent N messages
            archive_instead_of_delete: If True, archive messages; if False, delete them

        Returns:
            Dict with consolidation results
        """
        now_ms = int(time.time() * 1000)
        last_interaction = self.memory.get_last_interaction_ms()
        message_count = self.memory.get_message_count()

        results = {
            "ran": False,
            "reason": None,
            "facts_extracted": 0,
            "messages_summarized": 0,
            "messages_removed": 0,
        }

        # Check if consolidation needed
        silence_ms = now_ms - last_interaction if last_interaction else 0
        needs_consolidation = (
            (silence_ms > silence_threshold_ms and message_count > keep_recent_messages) or
            message_count > max_messages_before_consolidation
        )

        if not needs_consolidation:
            return results

        results["ran"] = True
        results["reason"] = "silence" if silence_ms > silence_threshold_ms else "message_count"

        # Get messages to consolidate (older than recent window)
        cutoff_ms = now_ms - silence_threshold_ms
        old_messages = self.memory.get_messages_for_summarization(
            older_than_ms=cutoff_ms,
            limit=200,
        )

        if not old_messages:
            return results

        logger.info("Consolidating %d messages", len(old_messages))

        # Extract facts
        facts = self.extract_facts_from_messages(old_messages, store=True)
        results["facts_extracted"] = len(facts)

        # Summarize
        summary = self.summarize_messages(old_messages, store=True)
        if summary:
            results["messages_summarized"] = len(old_messages)

            # Remove old messages (archive or delete based on setting)
            newest_timestamp = max(m.timestamp for m in old_messages)
            if archive_instead_of_delete:
                removed = self.memory.archive_messages_before(newest_timestamp + 1)
            else:
                removed = self.memory.delete_messages_before(newest_timestamp + 1)
            results["messages_removed"] = removed

        logger.info("Consolidation complete: %s", results)
        return results
