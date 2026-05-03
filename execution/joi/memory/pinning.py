"""
Core/pinned fact policy.

Pinned facts are always injected into LLM context. A fact is pinned when ANY
of these signals fire (in priority order):

  1. Explicit override in the `pinned_override` column on `user_facts`:
       - 1     -> explicitly pinned   (always wins)
       - 0     -> explicitly unpinned (always wins)
       - NULL  -> fall through to signals 2-3
  2. `source = 'admin'` — facts entered by the operator via `joi-admin facts add`
     are treated as curated and pin themselves. Operator can still override
     with `joi-admin facts unpin <id>` (sets pinned_override=0).
  3. Hardcoded `(category, key)` whitelist (`CORE_FACT_KEYS`) — auto-pinning by
     category for things every conversation needs (name, language, allergies).

Bounded by `MAX_CORE_FACTS` per conversation to prevent prompt-budget overflow.
"""

from __future__ import annotations
from typing import Optional


CORE_FACT_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("personal", "name"),
    ("personal", "gender"),
    ("personal", "preferred_language"),
    ("personal", "date_of_birth"),
    ("personal", "location_city"),
    ("health", "critical_allergy"),
    ("health", "critical_condition"),
})

MAX_CORE_FACTS: int = 20


def is_pinned(
    category: str,
    key: str,
    pinned_override: Optional[int],
    source: Optional[str] = None,
) -> bool:
    """Return True if this fact should be treated as core/pinned.

    Logic (matches pinned_filter_sql_clause):
      - pinned_override = 1                  -> True
      - pinned_override = 0                  -> False
      - source = 'admin'                     -> True
      - (category, key) in CORE_FACT_KEYS    -> True
      - otherwise                            -> False
    """
    if pinned_override == 1:
        return True
    if pinned_override == 0:
        return False
    if source == "admin":
        return True
    return (category, key) in CORE_FACT_KEYS


def pinned_filter_sql_clause() -> tuple[str, list]:
    """Return (sql_fragment, params) for the full pin filter on user_facts.

    Mirrors `is_pinned()`. Wrap with NOT(...) to get the unpinned set.

    Example output:
        sql    = "(pinned_override = 1 OR (pinned_override IS NULL AND
                  (source = 'admin' OR (category, key) IN ((?,?),(?,?), ...))))"
        params = ["health", "critical_allergy", "health", "critical_condition", ...]
    """
    pairs = sorted(CORE_FACT_KEYS)
    placeholders = ",".join("(?,?)" for _ in pairs)
    sql = (
        "(pinned_override = 1 OR "
        "(pinned_override IS NULL AND "
        f"(source = 'admin' OR (category, key) IN ({placeholders}))))"
    )
    params: list = []
    for cat, key in pairs:
        params.append(cat)
        params.append(key)
    return sql, params
