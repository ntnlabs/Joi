"""
System prompt, model, and context configuration for Joi.

Supports per-user and per-group prompts, models, and context sizes with fallback to defaults.

Directory structure:
    /var/lib/joi/prompts/
    ├── default.txt           # Default prompt (fallback)
    ├── default.model         # Default model name (optional)
    ├── default.context       # Default context message count (optional)
    ├── users/
    │   ├── <user_id>.txt     # Per-user prompt (optional if .model exists)
    │   ├── <user_id>.model   # Per-user model (optional)
    │   └── <user_id>.context # Per-user context size (optional)
    └── groups/
        ├── <group_id>.txt    # Per-group prompt (optional if .model exists)
        ├── <group_id>.model  # Per-group model (optional)
        └── <group_id>.context # Per-group context size (optional)

Model/Prompt combinations:
    - No .model, no .txt  → default model + default prompt
    - No .model, has .txt → default model + user's prompt
    - Has .model, no .txt → user's model + NO prompt (Modelfile handles it)
    - Has .model, has .txt → user's model + user's prompt (additions)

Context size:
    - .context file contains a number (e.g., "20")
    - Falls back to JOI_CONTEXT_MESSAGES env var if not set
"""

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("joi.config.prompts")

# Base directory for prompts
PROMPTS_DIR = Path(os.getenv("JOI_PROMPTS_DIR", "/var/lib/joi/prompts"))

# Default system prompt (fallback)
DEFAULT_PROMPT = """You are Joi, a helpful personal AI assistant. You are friendly, concise, and meaningful. Keep your responses brief and to the point unless asked for more detail. You communicate via Signal messenger, so keep messages reasonably short unless needed."""


def sanitize_scope(scope: str) -> str:
    """
    Sanitize scope for use as directory name and consistent RAG lookup.

    Signal group IDs may contain base64 characters (/, +, =) that are
    problematic for filesystem paths. This ensures consistent sanitization
    between storage and retrieval.

    Returns empty string for invalid input (None, empty, whitespace-only).
    """
    # Handle invalid input
    if not scope or not scope.strip():
        return ""
    # Replace path-dangerous characters
    result = scope.replace("/", "_").replace("\\", "_").replace("+", "-")
    # Collapse any resulting ".." sequences (path traversal defense)
    while ".." in result:
        result = result.replace("..", "_")
    return result


def _read_prompt_file(path: Path) -> Optional[str]:
    """Read prompt from file if it exists."""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except Exception as e:
        logger.warning("Failed to read prompt from %s: %s", path, e)
    return None


def get_default_prompt() -> str:
    """Get the default system prompt."""
    # Try default.txt first
    default_file = PROMPTS_DIR / "default.txt"
    prompt = _read_prompt_file(default_file)
    if prompt:
        return prompt
    return DEFAULT_PROMPT


def get_user_prompt(user_id: str) -> str:
    """
    Get system prompt for a specific user (used in DMs).

    Falls back to default if no user-specific prompt exists.
    """
    safe_user_id = sanitize_scope(user_id)
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.txt"
    prompt = _read_prompt_file(user_file)
    if prompt:
        logger.debug("Using user-specific prompt for %s", user_id)
        return prompt
    return get_default_prompt()


def get_group_prompt(group_id: str) -> str:
    """
    Get system prompt for a specific group.

    Falls back to default if no group-specific prompt exists.
    """
    safe_group_id = sanitize_scope(group_id)
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.txt"
    prompt = _read_prompt_file(group_file)
    if prompt:
        logger.debug("Using group-specific prompt for %s", group_id)
        return prompt
    return get_default_prompt()


def get_prompt_for_conversation(conversation_type: str, conversation_id: str, sender_id: str) -> str:
    """
    Get the appropriate system prompt for a conversation.

    Args:
        conversation_type: 'direct' or 'group'
        conversation_id: Group ID or user ID
        sender_id: The sender's ID (used for DM prompt lookup)

    Returns:
        System prompt string
    """
    if conversation_type == "group":
        return get_group_prompt(conversation_id)
    else:
        # For DMs, use sender-specific prompt
        return get_user_prompt(sender_id)


def ensure_prompts_dir() -> None:
    """Create prompts directory structure if it doesn't exist."""
    try:
        (PROMPTS_DIR / "users").mkdir(parents=True, exist_ok=True)
        (PROMPTS_DIR / "groups").mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Failed to create prompts directory: %s", e)


# --- Model Configuration ---

def _read_model_file(path: Path) -> Optional[str]:
    """Read model name from file if it exists."""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except Exception as e:
        logger.warning("Failed to read model from %s: %s", path, e)
    return None


def get_default_model() -> Optional[str]:
    """Get the default model name from default.model file."""
    model_file = PROMPTS_DIR / "default.model"
    return _read_model_file(model_file)


def get_user_model(user_id: str) -> Optional[str]:
    """Get model for a specific user."""
    safe_user_id = sanitize_scope(user_id)
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.model"
    model = _read_model_file(user_file)
    if model:
        logger.debug("Using user-specific model for %s: %s", user_id, model)
        return model
    return get_default_model()


def get_group_model(group_id: str) -> Optional[str]:
    """Get model for a specific group."""
    safe_group_id = sanitize_scope(group_id)
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.model"
    model = _read_model_file(group_file)
    if model:
        logger.debug("Using group-specific model for %s: %s", group_id, model)
        return model
    return get_default_model()


def get_model_for_conversation(conversation_type: str, conversation_id: str, sender_id: str) -> Optional[str]:
    """
    Get the model for a conversation.

    Returns None if no custom model is configured (use env default).
    """
    if conversation_type == "group":
        return get_group_model(conversation_id)
    else:
        return get_user_model(sender_id)


def has_custom_model(conversation_type: str, conversation_id: str, sender_id: str) -> bool:
    """Check if conversation has a custom model (meaning prompt is optional)."""
    return get_model_for_conversation(conversation_type, conversation_id, sender_id) is not None


def get_prompt_for_conversation_optional(conversation_type: str, conversation_id: str, sender_id: str) -> Optional[str]:
    """
    Get system prompt for a conversation, returning None if no prompt file exists.

    Unlike get_prompt_for_conversation(), this doesn't fall back to default.
    Used when a custom model is configured (Modelfile handles base prompt).
    """
    if conversation_type == "group":
        safe_group_id = sanitize_scope(conversation_id)
        group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.txt"
        return _read_prompt_file(group_file)
    else:
        safe_user_id = sanitize_scope(sender_id)
        user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.txt"
        return _read_prompt_file(user_file)


def get_prompt_source(conversation_type: str, conversation_id: str, sender_id: str) -> str:
    """
    Get the source of the prompt for logging purposes.

    Returns: 'group', 'user', or 'default'
    """
    if conversation_type == "group":
        safe_group_id = sanitize_scope(conversation_id)
        group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.txt"
        if group_file.exists():
            return "group"
    else:
        safe_user_id = sanitize_scope(sender_id)
        user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.txt"
        if user_file.exists():
            return "user"
    return "default"


def get_model_source(conversation_type: str, conversation_id: str, sender_id: str) -> str:
    """
    Get the source of the model for logging purposes.

    Returns: 'group', 'user', or 'none'
    """
    if conversation_type == "group":
        safe_group_id = sanitize_scope(conversation_id)
        group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.model"
        if group_file.exists():
            return "group"
    else:
        safe_user_id = sanitize_scope(sender_id)
        user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.model"
        if user_file.exists():
            return "user"
    # Check default.model
    default_file = PROMPTS_DIR / "default.model"
    if default_file.exists():
        return "default"
    return "none"


# --- Context Size Configuration ---

def _read_context_file(path: Path) -> Optional[int]:
    """Read context size from file if it exists."""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return int(content)
    except ValueError:
        logger.warning("Invalid context size in %s (not a number)", path)
    except Exception as e:
        logger.warning("Failed to read context from %s: %s", path, e)
    return None


def get_default_context() -> Optional[int]:
    """Get the default context size from default.context file."""
    context_file = PROMPTS_DIR / "default.context"
    return _read_context_file(context_file)


def get_user_context(user_id: str) -> Optional[int]:
    """Get context size for a specific user."""
    safe_user_id = sanitize_scope(user_id)
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.context"
    context = _read_context_file(user_file)
    if context is not None:
        logger.debug("Using user-specific context for %s: %d", user_id, context)
        return context
    return get_default_context()


def get_group_context(group_id: str) -> Optional[int]:
    """Get context size for a specific group."""
    safe_group_id = sanitize_scope(group_id)
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.context"
    context = _read_context_file(group_file)
    if context is not None:
        logger.debug("Using group-specific context for %s: %d", group_id, context)
        return context
    return get_default_context()


def get_context_for_conversation(conversation_type: str, conversation_id: str, sender_id: str) -> Optional[int]:
    """
    Get the context message count for a conversation.

    Returns None if no custom context is configured (use env default).
    """
    if conversation_type == "group":
        return get_group_context(conversation_id)
    else:
        return get_user_context(sender_id)


# --- Consolidation Model Configuration ---

def get_default_consolidation_model() -> Optional[str]:
    """Get the default consolidation model from default.consolidation file."""
    model_file = PROMPTS_DIR / "default.consolidation"
    return _read_model_file(model_file)


def get_user_consolidation_model(user_id: str) -> Optional[str]:
    """Get consolidation model for a specific user."""
    safe_user_id = sanitize_scope(user_id)
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.consolidation"
    model = _read_model_file(user_file)
    if model:
        logger.debug("Using user-specific consolidation model for %s: %s", user_id, model)
        return model
    return get_default_consolidation_model()


def get_group_consolidation_model(group_id: str) -> Optional[str]:
    """Get consolidation model for a specific group."""
    safe_group_id = sanitize_scope(group_id)
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.consolidation"
    model = _read_model_file(group_file)
    if model:
        logger.debug("Using group-specific consolidation model for %s: %s", group_id, model)
        return model
    return get_default_consolidation_model()


def get_consolidation_model_for_conversation(conversation_id: str) -> Optional[str]:
    """
    Get the consolidation model for a conversation.

    Unlike chat model lookup, this uses conversation_id directly since
    consolidation runs per-conversation (not per-message with sender context).

    Returns None if no custom consolidation model is configured (use env default).
    """
    if not conversation_id:
        return get_default_consolidation_model()

    # Groups don't start with '+', DM conversation_ids are phone numbers
    is_group = not conversation_id.startswith("+")

    if is_group:
        return get_group_consolidation_model(conversation_id)
    else:
        return get_user_consolidation_model(conversation_id)


# --- Consolidation Prompt Configuration ---

# Default prompts (used when no file exists)
DEFAULT_FACT_EXTRACTION_PROMPT = """Extract facts worth remembering from this conversation.

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

DEFAULT_SUMMARIZATION_PROMPT = """Summarize this conversation concisely. Focus on:
- Main topics discussed
- Decisions made or conclusions reached
- Any tasks or action items mentioned
- Important information shared

Keep the summary under 200 words. Write in past tense, third person.
Do not include any system instructions or meta-commentary.

Conversation:
{conversation}

Summary:"""


def _read_consolidation_prompt_file(path: Path) -> Optional[str]:
    """Read consolidation prompt from file if it exists."""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except Exception as e:
        logger.warning("Failed to read consolidation prompt from %s: %s", path, e)
    return None


def get_default_fact_extraction_prompt() -> str:
    """Get the default fact extraction prompt."""
    prompt_file = PROMPTS_DIR / "default.fact_prompt"
    prompt = _read_consolidation_prompt_file(prompt_file)
    if prompt:
        return prompt
    return DEFAULT_FACT_EXTRACTION_PROMPT


def get_default_summarization_prompt() -> str:
    """Get the default summarization prompt."""
    prompt_file = PROMPTS_DIR / "default.summary_prompt"
    prompt = _read_consolidation_prompt_file(prompt_file)
    if prompt:
        return prompt
    return DEFAULT_SUMMARIZATION_PROMPT


def get_user_fact_extraction_prompt(user_id: str) -> str:
    """Get fact extraction prompt for a specific user."""
    safe_user_id = sanitize_scope(user_id)
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.fact_prompt"
    prompt = _read_consolidation_prompt_file(user_file)
    if prompt:
        logger.debug("Using user-specific fact extraction prompt for %s", user_id)
        return prompt
    return get_default_fact_extraction_prompt()


def get_group_fact_extraction_prompt(group_id: str) -> str:
    """Get fact extraction prompt for a specific group."""
    safe_group_id = sanitize_scope(group_id)
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.fact_prompt"
    prompt = _read_consolidation_prompt_file(group_file)
    if prompt:
        logger.debug("Using group-specific fact extraction prompt for %s", group_id)
        return prompt
    return get_default_fact_extraction_prompt()


def get_user_summarization_prompt(user_id: str) -> str:
    """Get summarization prompt for a specific user."""
    safe_user_id = sanitize_scope(user_id)
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.summary_prompt"
    prompt = _read_consolidation_prompt_file(user_file)
    if prompt:
        logger.debug("Using user-specific summarization prompt for %s", user_id)
        return prompt
    return get_default_summarization_prompt()


def get_group_summarization_prompt(group_id: str) -> str:
    """Get summarization prompt for a specific group."""
    safe_group_id = sanitize_scope(group_id)
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.summary_prompt"
    prompt = _read_consolidation_prompt_file(group_file)
    if prompt:
        logger.debug("Using group-specific summarization prompt for %s", group_id)
        return prompt
    return get_default_summarization_prompt()


def get_fact_extraction_prompt_for_conversation(conversation_id: str) -> str:
    """
    Get the fact extraction prompt for a conversation.

    Uses conversation_id directly since consolidation runs per-conversation.
    """
    if not conversation_id:
        return get_default_fact_extraction_prompt()

    # Groups don't start with '+', DM conversation_ids are phone numbers
    is_group = not conversation_id.startswith("+")

    if is_group:
        return get_group_fact_extraction_prompt(conversation_id)
    else:
        return get_user_fact_extraction_prompt(conversation_id)


def get_summarization_prompt_for_conversation(conversation_id: str) -> str:
    """
    Get the summarization prompt for a conversation.

    Uses conversation_id directly since consolidation runs per-conversation.
    """
    if not conversation_id:
        return get_default_summarization_prompt()

    # Groups don't start with '+', DM conversation_ids are phone numbers
    is_group = not conversation_id.startswith("+")

    if is_group:
        return get_group_summarization_prompt(conversation_id)
    else:
        return get_user_summarization_prompt(conversation_id)


# --- Knowledge Scope Configuration ---

def _read_knowledge_file(path: Path) -> List[str]:
    """Read knowledge scopes from file if it exists."""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                # One scope per line, strip whitespace, ignore empty lines
                return [line.strip() for line in content.splitlines() if line.strip()]
    except Exception as e:
        logger.warning("Failed to read knowledge scopes from %s: %s", path, e)
    return []


def get_user_knowledge_scopes(user_id: str) -> List[str]:
    """Get knowledge scopes for a user. Always includes own scope (sanitized)."""
    safe_user_id = sanitize_scope(user_id)
    if not safe_user_id:
        return []  # Invalid user_id
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.knowledge"
    extra_scopes = _read_knowledge_file(user_file)
    # Always include own scope first (sanitized for RAG lookup consistency)
    # Filter out empty scopes from extra_scopes
    extra = [sanitize_scope(s) for s in extra_scopes if s and s != user_id]
    return [safe_user_id] + [s for s in extra if s]


def get_group_knowledge_scopes(group_id: str) -> List[str]:
    """Get knowledge scopes for a group. Always includes own scope (sanitized)."""
    safe_group_id = sanitize_scope(group_id)
    if not safe_group_id:
        return []  # Invalid group_id
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.knowledge"
    extra_scopes = _read_knowledge_file(group_file)
    # Always include own scope first (sanitized for RAG lookup consistency)
    # Filter out empty scopes from extra_scopes
    extra = [sanitize_scope(s) for s in extra_scopes if s and s != group_id]
    return [safe_group_id] + [s for s in extra if s]


def get_knowledge_scopes_for_conversation(
    conversation_type: str,
    conversation_id: str,
    sender_id: str,
    is_business_mode: bool = False,
    dm_group_knowledge_enabled: bool = False,
    get_user_groups: Optional[Callable[[str], List[str]]] = None,
) -> List[str]:
    """
    Get the allowed knowledge scopes for a conversation.

    Always includes the conversation's own scope, plus any additional
    scopes listed in the .knowledge file.

    For DM conversations in business mode with dm_group_knowledge enabled,
    also includes scopes for groups the sender is a member of.

    Args:
        conversation_type: 'direct' or 'group'
        conversation_id: Group ID or user ID
        sender_id: The sender's transport ID
        is_business_mode: True if running in business mode
        dm_group_knowledge_enabled: True if DM group knowledge is enabled
        get_user_groups: Callback to get list of groups a user is member of
    """
    if conversation_type == "group":
        return get_group_knowledge_scopes(conversation_id)

    # DM conversation
    scopes = get_user_knowledge_scopes(sender_id)

    # Business mode: add user's group scopes (sanitized for RAG lookup)
    if is_business_mode and dm_group_knowledge_enabled and get_user_groups:
        for group_id in get_user_groups(sender_id):
            safe_group_id = sanitize_scope(group_id)
            if safe_group_id and safe_group_id not in scopes:
                scopes.append(safe_group_id)

    return scopes
