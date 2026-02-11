"""
System prompt configuration for Joi.

Supports per-user and per-group prompts with fallback to defaults.

Directory structure:
    /var/lib/joi/prompts/
    ├── default.txt           # Default prompt for all
    ├── users/
    │   └── <user_id>.txt     # Per-user prompt (for DMs)
    └── groups/
        └── <group_id>.txt    # Per-group prompt
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("joi.config.prompts")

# Base directory for prompts
PROMPTS_DIR = Path(os.getenv("JOI_PROMPTS_DIR", "/var/lib/joi/prompts"))

# Default system prompt (fallback)
DEFAULT_PROMPT = """You are Joi, a helpful personal AI assistant. You are friendly, concise, and meaningful. Keep your responses brief and to the point unless asked for more detail. You communicate via Signal messenger, so keep messages reasonably short unless needed."""


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
    user_file = PROMPTS_DIR / "users" / f"{user_id}.txt"
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
    # Sanitize group_id for filename (base64 can have / and +)
    safe_group_id = group_id.replace("/", "_").replace("+", "-")
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
