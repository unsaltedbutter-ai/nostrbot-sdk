"""Per-user asyncio.Lock manager with idle cleanup.

NostrBot uses this to serialize DM processing per sender: two DMs from the
same user (often the same message via NIP-04 and NIP-17 in parallel) can't
race through state transitions.

Locks are lazily created on first request, tracked by last-use time, and
reaped after `idle_seconds` of inactivity provided they are not currently
held. Without cleanup, a long-running bot grows one Lock per pubkey it has
ever spoken to.
"""

from __future__ import annotations

import asyncio
import time


class UserLockManager:
    """Maintain one asyncio.Lock per string key (e.g., sender hex pubkey).

    `get(key)` returns the Lock for that key, creating it if needed.
    `cleanup_idle()` removes locks whose last-use time is older than the
    configured idle threshold and which are not currently held. Held locks
    are always preserved regardless of idle time.
    """

    def __init__(self, idle_seconds: float = 300.0) -> None:
        self._idle = idle_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}

    def get(self, key: str) -> asyncio.Lock:
        """Return the Lock for `key`, creating it on first call.

        Updates the last-use time on every call so cleanup correctly skips
        recently-touched locks even if the lock is not currently held.
        """
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._last_used[key] = time.monotonic()
        return lock

    def cleanup_idle(self) -> int:
        """Remove idle, unheld locks. Returns count removed.

        Safe to call periodically (e.g., every 5 minutes). Locks currently
        held are always retained, even if their last-use time is stale: a
        long-running handler shouldn't have its lock yanked out from under it.
        """
        now = time.monotonic()
        to_remove: list[str] = []
        for key, last in self._last_used.items():
            if now - last > self._idle:
                lock = self._locks.get(key)
                if lock is not None and not lock.locked():
                    to_remove.append(key)
        for key in to_remove:
            del self._locks[key]
            del self._last_used[key]
        return len(to_remove)

    def __len__(self) -> int:
        return len(self._locks)

    def __contains__(self, key: str) -> bool:
        return key in self._locks
