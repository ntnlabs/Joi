"""
System prompt and model configuration for Joi.

Supports per-user and per-group prompts and models with fallback to defaults.

Directory structure:
    /var/lib/joi/prompts/
    ├── default.txt           # Default prompt (fallback)
    ├── default.model         # Default model name (optional)
    ├── users/
    │   ├── <user_id>.txt     # Per-user prompt (optional if .model exists)
    │   └── <user_id>.model   # Per-user model (optional)
    └── groups/
        ├── <group_id>.txt    # Per-group prompt (optional if .model exists)
        └── <group_id>.model  # Per-group model (optional)

Model/Prompt combinations:
    - No .model, no .txt  → default model + default prompt
    - No .model, has .txt → default model + user's prompt
    - Has .model, no .txt → user's model + NO prompt (Modelfile handles it)
    - Has .model, has .txt → user's model + user's prompt (additions)
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
    user_file = PROMPTS_DIR / "users" / f"{user_id}.model"
    model = _read_model_file(user_file)
    if model:
        logger.debug("Using user-specific model for %s: %s", user_id, model)
        return model
    return get_default_model()


def get_group_model(group_id: str) -> Optional[str]:
    """Get model for a specific group."""
    safe_group_id = group_id.replace("/", "_").replace("+", "-")
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
        safe_group_id = conversation_id.replace("/", "_").replace("+", "-")
        group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.txt"
        return _read_prompt_file(group_file)
    else:
        user_file = PROMPTS_DIR / "users" / f"{sender_id}.txt"
        return _read_prompt_file(user_file)
