"""
Core/pinned fact policy.

Pinned facts are always injected into LLM context. Membership comes from:
1. A hardcoded `(category, key)` whitelist (`CORE_FACT_KEYS`) — auto-pinning by category.
2. An explicit `pinned_override` column on `user_facts`:
     - NULL  -> use whitelist
     - 0     -> explicitly unpinned (overrides whitelist)
     - 1     -> explicitly pinned   (overrides whitelist)

Bounded by `MAX_CORE_FACTS` per conversation to prevent prompt-budget overflow.
"""

from __future__ import annotations
from typing import Optional


CORE_FACT_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("personal", "name"),
    ("personal", "preferred_language"),
    ("personal", "pronoun"),
    ("personal", "date_of_birth"),
    ("personal", "location_city"),
    ("health", "critical_allergy"),
    ("health", "critical_condition"),
})

MAX_CORE_FACTS: int = 20


def is_pinned(category: str, key: str, pinned_override: Optional[int]) -> bool:
    """Return True if this fact should be treated as core/pinned.

    Logic:
      - pinned_override = 1 -> True (explicit pin)
      - pinned_override = 0 -> False (explicit unpin)
      - pinned_override = NULL -> whitelist lookup on (category, key)
    """
    if pinned_override == 1:
        return True
    if pinned_override == 0:
        return False
    return (category, key) in CORE_FACT_KEYS
