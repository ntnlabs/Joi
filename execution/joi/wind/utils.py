"""
Shared datetime utilities for Wind modules.
"""

from datetime import datetime, timezone
from typing import Optional


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO format datetime string, returning a UTC-aware datetime.

    Naive datetimes (legacy DB values stored before timezone tracking) are
    assumed to be server local time and converted to UTC via astimezone().
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime to ISO string for DB storage."""
    if not dt:
        return None
    return dt.isoformat()
