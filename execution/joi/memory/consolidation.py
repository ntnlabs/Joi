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
FACT_EXTRACTION_PROMPT = """Extract facts about the user from this conversation.

IMPORTANT: Return ONLY a valid JSON array. No explanations, no markdown, no text before or after.

Each fact must have exactly these 4 fields:
- "category": one of: "personal", "preference", "relationship", "work", "routine", "interest"
- "key": short identifier like "name", "job", "hobby", "partner_name"
- "value": the actual fact as a string
- "confidence": number between 0.0 and 1.0

If no facts found, return exactly: []

Example of correct output format:
[{{"category": "personal", "key": "name", "value": "Peter", "confidence": 1.0}}, {{"category": "preference", "key": "coffee", "value": "likes black coffee", "confidence": 0.8}}]

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

        try:
            response = self.llm.generate(prompt=prompt)
        except Exception as e:
            logger.error("LLM generate failed: %s", e)
            return []

        if response.error:
            logger.error("LLM error during fact extraction: %s", response.error)
            return []

        logger.debug("LLM response for facts: %s", response.text[:500] if response.text else "(empty)")

        try:
            facts = parse_facts_json(response.text)
        except Exception as e:
            logger.error("parse_facts_json failed: %s", e)
            return []

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
                retry_response = self.llm.generate(prompt=retry_prompt)
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
            # Get conversation_id and sender from first message
            convo_id = messages[0].conversation_id if messages else ""
            # For groups (messages from multiple senders), we can't reliably attribute facts
            # Only store if single sender or DM (all messages from same person)
            sender_ids = set(m.sender_id for m in messages if m.sender_id and m.direction == "inbound")

            if len(sender_ids) == 1:
                # Single sender - safe to store facts
                sender_id = sender_ids.pop()
                # Determine if this is a group or DM based on channel
                is_group = any(m.channel == "group" for m in messages)
                # Use composite key for groups: conversation_id:sender_id
                # For DMs, use just conversation_id (matches live server behavior)
                if is_group:
                    fact_key = f"{convo_id}:{sender_id}" if sender_id and convo_id else convo_id or ""
                else:
                    fact_key = convo_id or ""

                stored_count = 0
                for fact in valid_facts:
                    try:
                        self.memory.store_fact(
                            category=fact["category"],
                            key=fact["key"],
                            value=str(fact["value"]),
                            confidence=float(fact.get("confidence", 0.8)),
                            source="inferred",
                            conversation_id=fact_key,
                        )
                        stored_count += 1
                    except (KeyError, TypeError, ValueError) as e:
                        logger.warning("Skipping malformed fact %s: %s", fact, e)
                if stored_count:
                    logger.info("Extracted and stored %d facts for %s", stored_count, fact_key)
            else:
                # Multiple senders - skip storing to avoid mixing facts
                logger.info("Skipping fact storage for mixed-sender batch (%d senders)", len(sender_ids))

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
        silence_threshold_ms: int = 3600000,  # 1 hour
        max_messages_before_consolidation: int = 200,
        context_messages: int = 40,
        archive_instead_of_delete: bool = False,
    ) -> Dict[str, Any]:
        """
        Run full memory consolidation per conversation.

        Triggers per conversation when:
        - Silence for more than threshold, OR
        - More than max_messages in that conversation

        Args:
            silence_threshold_ms: Consider conversation ended after this much silence
            max_messages_before_consolidation: Force consolidation at this count
            context_messages: Always preserve the most recent N messages per conversation
            archive_instead_of_delete: If True, archive messages; if False, delete them

        Returns:
            Dict with consolidation results (totals across all conversations)
        """
        now_ms = int(time.time() * 1000)

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
                now_ms=now_ms,
                silence_threshold_ms=silence_threshold_ms,
                max_messages=max_messages_before_consolidation,
                context_messages=context_messages,
                archive_instead_of_delete=archive_instead_of_delete,
            )

            if convo_results["ran"]:
                results["ran"] = True
                results["reason"] = convo_results["reason"]
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
        now_ms: int,
        silence_threshold_ms: int,
        max_messages: int,
        context_messages: int,
        archive_instead_of_delete: bool,
    ) -> Dict[str, Any]:
        """Consolidate a single conversation."""
        results = {
            "ran": False,
            "reason": None,
            "facts_extracted": 0,
            "messages_summarized": 0,
            "messages_removed": 0,
        }

        # Check message count and last interaction for this conversation
        message_count = self.memory.get_message_count_for_conversation(conversation_id)
        last_interaction = self.memory.get_last_interaction_for_conversation(conversation_id)

        silence_ms = now_ms - last_interaction if last_interaction else 0
        needs_consolidation = (
            (silence_ms > silence_threshold_ms and message_count > context_messages) or
            message_count > max_messages
        )

        if not needs_consolidation:
            return results

        results["ran"] = True
        results["reason"] = "silence" if silence_ms > silence_threshold_ms else "message_count"

        # Get messages for this conversation, preserving context window
        old_messages = self.memory.get_messages_for_summarization(
            exclude_recent=context_messages,
            limit=200,
            conversation_id=conversation_id,
        )

        if not old_messages:
            return results

        logger.info("Consolidating %d messages for conversation %s", len(old_messages), conversation_id)

        # Extract facts (with error handling)
        try:
            facts = self.extract_facts_from_messages(old_messages, store=True)
            results["facts_extracted"] = len(facts)
        except Exception as e:
            logger.error("Fact extraction failed for %s: %s", conversation_id, e, exc_info=True)
            results["facts_extracted"] = 0

        # Summarize (with error handling)
        try:
            summary = self.summarize_messages(old_messages, store=True)
        except Exception as e:
            logger.error("Summarization failed for %s: %s", conversation_id, e, exc_info=True)
            summary = None

        if summary:
            results["messages_summarized"] = len(old_messages)

            # Remove old messages for this conversation
            newest_timestamp = max(m.timestamp for m in old_messages)
            if archive_instead_of_delete:
                removed = self.memory.archive_messages_before(newest_timestamp + 1, conversation_id)
            else:
                removed = self.memory.delete_messages_before(newest_timestamp + 1, conversation_id)
            results["messages_removed"] = removed

        return results
