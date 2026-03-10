"""Group membership cache for cross-group knowledge access control."""

import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

import httpx

from hmac_auth import create_request_headers

logger = logging.getLogger("joi.api.group_cache")


class GroupMembershipCache:
    """Cache of group memberships from signal-cli.

    Only active when business mode + dm_group_knowledge enabled.
    Queries mesh /groups/members endpoint to get real group membership.

    Security properties:
    - Fail-closed: If membership cannot be verified, access is denied
    - Single-flight refresh: Prevents thundering herd on cache expiry
    - Dual ID matching: Handles both phone numbers and UUIDs from signal-cli
    """

    def __init__(self):
        self._cache: Dict[str, List[str]] = {}  # group_id -> [member_ids]
        self._last_refresh: float = 0
        self._lock = threading.Lock()
        self._refreshing = False  # Single-flight flag to prevent thundering herd
        # Configurable via env (default 15 min, bounds: 1-1440)
        self._refresh_minutes = self._validate_refresh_minutes(
            os.getenv("JOI_MEMBERSHIP_REFRESH_MINUTES", "15")
        )
        # Dependencies (set via set_dependencies)
        self._mesh_url: Optional[str] = None
        self._policy_manager = None
        self._get_current_hmac_secret: Optional[Callable] = None

    def set_dependencies(
        self,
        mesh_url: str,
        policy_manager,
        get_current_hmac_secret: Callable,
    ):
        """Set dependencies after construction."""
        self._mesh_url = mesh_url
        self._policy_manager = policy_manager
        self._get_current_hmac_secret = get_current_hmac_secret

    @staticmethod
    def _validate_refresh_minutes(value: str) -> int:
        """Validate refresh minutes config with reasonable bounds."""
        try:
            minutes = int(value)
        except ValueError:
            logger.warning("Invalid JOI_MEMBERSHIP_REFRESH_MINUTES, using default 15", extra={"value": value})
            return 15
        # Bounds: 1 minute (aggressive) to 1440 minutes (24 hours)
        if minutes < 1:
            logger.warning("JOI_MEMBERSHIP_REFRESH_MINUTES too low, using minimum 1", extra={"value": minutes})
            return 1
        if minutes > 1440:
            logger.warning("JOI_MEMBERSHIP_REFRESH_MINUTES too high, using maximum 1440", extra={"value": minutes})
            return 1440
        return minutes

    def _should_be_active(self) -> bool:
        """Only run when the attack vector exists (business mode + dm_group_knowledge)."""
        if not self._policy_manager:
            return False
        return (self._policy_manager.is_business_mode() and
                self._policy_manager.is_dm_group_knowledge_enabled())

    def _refresh_unlocked(self) -> bool:
        """Fetch fresh membership from mesh. Caller must NOT hold lock."""
        if not self._should_be_active():
            return False  # Skip - not needed

        if not self._mesh_url:
            return False

        try:
            url = f"{self._mesh_url}/groups/members"
            headers = {"Content-Type": "application/json"}
            if self._get_current_hmac_secret:
                current_secret = self._get_current_hmac_secret()
                if current_secret:
                    hmac_headers = create_request_headers(b"", current_secret)
                    headers.update(hmac_headers)

            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("data", {})

            with self._lock:
                # Don't replace valid cache with empty response (signal-cli might be restarting)
                # Only update if: new data has groups, OR we have no cache yet
                if data or not self._cache:
                    self._cache = data
                    logger.debug("Refreshed group membership", extra={"group_count": len(data)})
                else:
                    logger.warning("Ignoring empty membership response", extra={"cached_groups": len(self._cache)})
                self._last_refresh = time.time()
            return True
        except Exception as e:
            logger.warning("Failed to refresh group membership", extra={"error": str(e)})
            return False

    def refresh(self) -> bool:
        """Public refresh method with single-flight protection."""
        # Single-flight: only one thread refreshes at a time
        with self._lock:
            if self._refreshing:
                # Another thread is already refreshing, wait for it
                return len(self._cache) > 0  # Return True if we have cache to use
            self._refreshing = True

        try:
            return self._refresh_unlocked()
        finally:
            with self._lock:
                self._refreshing = False

    def get_user_groups(self, user_id: str) -> List[str]:
        """Get list of groups where user is a member.

        Security: Fail-closed - if membership cannot be verified, returns empty list.
        """
        if not self._should_be_active():
            return []  # Feature disabled

        if not user_id:
            return []  # Invalid input

        refresh_seconds = self._refresh_minutes * 60

        # Hold lock for entire check-and-return to fix TOCTOU
        with self._lock:
            time_since_refresh = time.time() - self._last_refresh
            is_stale = time_since_refresh > refresh_seconds
            has_cache = len(self._cache) > 0

            # If cache is fresh, use it directly
            if not is_stale and has_cache:
                return self._find_user_groups_unlocked(user_id)

            # Need refresh - check if another thread is already doing it
            if self._refreshing:
                # Another thread is refreshing, use current cache (stale or not)
                if has_cache:
                    logger.debug("Using cache while another thread refreshes")
                    return self._find_user_groups_unlocked(user_id)
                else:
                    # No cache and refresh in progress - fail closed
                    logger.warning("No cache available, refresh in progress - denying access (fail-closed)", extra={"action": "access_denied"})
                    return []

            # We need to refresh - mark ourselves as refreshing
            self._refreshing = True

        # Release lock during HTTP call to avoid blocking other threads
        try:
            refresh_success = self._refresh_unlocked()
        finally:
            with self._lock:
                self._refreshing = False

        # Now check results with lock held
        with self._lock:
            if refresh_success:
                return self._find_user_groups_unlocked(user_id)

            # Refresh failed - re-check cache state (may have changed during HTTP call)
            current_has_cache = len(self._cache) > 0
            if current_has_cache:
                logger.warning("Using stale membership cache (refresh failed)", extra={"action": "cache_stale"})
                return self._find_user_groups_unlocked(user_id)
            else:
                # FAIL-CLOSED: No cache and refresh failed - deny access
                logger.warning("No membership cache and refresh failed - denying group access (fail-closed)", extra={"action": "access_denied"})
                return []

    def _find_user_groups_unlocked(self, user_id: str) -> List[str]:
        """Find groups for user. Caller must hold lock.

        Handles ID format mismatch by checking if user_id matches any member ID.
        Signal-cli may return phone numbers (+1234567890) or UUIDs.
        """
        result = []
        for gid, members in self._cache.items():
            for member_id in members:
                # Direct match (most common case)
                if user_id == member_id:
                    result.append(gid)
                    break
                # Normalized phone number match (handle +/no-+ variations)
                if self._phone_numbers_match(user_id, member_id):
                    result.append(gid)
                    break
        return result

    @staticmethod
    def _phone_numbers_match(id1: str, id2: str) -> bool:
        """Check if two IDs match as phone numbers (handling +prefix variations)."""
        # Only compare if both look like phone numbers
        if not (id1 and id2):
            return False
        # Normalize: strip + prefix for comparison
        n1 = id1.lstrip('+')
        n2 = id2.lstrip('+')
        # Only match if they're numeric (phone numbers, not UUIDs)
        if n1.isdigit() and n2.isdigit():
            return n1 == n2
        return False

    def get_cache_age_seconds(self) -> float:
        """Get age of cache in seconds."""
        with self._lock:
            if self._last_refresh == 0:
                return -1  # Never refreshed
            return time.time() - self._last_refresh

    def get_cache_size(self) -> int:
        """Get number of groups in cache."""
        with self._lock:
            return len(self._cache)
