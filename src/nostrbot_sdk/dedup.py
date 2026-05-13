"""Time-bounded deduplication set.

Used by NostrBot for two purposes:
  - EventDedup: keys are event_id_hex (str), default TTL 600s. Catches relay
    redelivery (the same event arriving multiple times via different relays
    or on reconnection).
  - ContentDedup: keys are (sender_hex, content_str) tuples, default TTL 30s.
    Catches the same DM arriving via both NIP-04 and NIP-17 (different event
    ids, same payload). Short TTL so legitimate retries after a failure
    still get through.

Both behaviors are the same generic class with different key types and TTLs.
"""

from __future__ import annotations

import time
from typing import Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)


class Dedup(Generic[K]):
    """Time-bounded set: remember keys for `ttl_seconds`, drop older ones.

    Thread-unsafe but coroutine-safe in CPython (dict ops are atomic and we
    tolerate benign races on write).

    Pruning is amortized: every `prune_every` calls to check_and_add we walk
    the dict and drop stale entries. Avoids O(n) on every event while still
    bounding memory for long-running bots.
    """

    def __init__(self, ttl_seconds: float, prune_every: int = 50) -> None:
        self._ttl = ttl_seconds
        self._prune_every = prune_every
        self._seen: dict[K, float] = {}
        self._call_count = 0

    def check_and_add(self, key: K) -> bool:
        """Atomic check-and-set.

        Returns True if `key` was already in the set (caller should skip).
        Returns False if `key` is new (now registered for future checks).
        """
        now = time.monotonic()
        self._call_count += 1
        if self._call_count >= self._prune_every:
            self._call_count = 0
            self._prune(now)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def forget(self, key: K) -> None:
        """Remove `key` if present. No-op otherwise.

        Useful when a handler fails: forget the entry so the user can retry
        without hitting the dedup. Calling sites typically wrap the handler
        in try/except and forget() on exception.
        """
        self._seen.pop(key, None)

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        stale = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in stale:
            del self._seen[k]

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, key: K) -> bool:
        return key in self._seen
