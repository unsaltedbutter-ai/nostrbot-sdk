"""Kind 0 (Metadata) cache with TTL.

Bots commonly need a sender's display name, lud16, or picture beyond their
hex pubkey. Doing a relay fetch on every DM is slow and rude; doing it
inline ad-hoc means duplicated code. `IdentityResolver` does it once per
pubkey per TTL window.

Returns an `Identity` dataclass on every call: when no kind 0 is found,
only `pubkey_hex` is populated and all other fields are None. The negative
result is also cached to avoid hammering relays for users with no profile.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from nostr_sdk import Filter, Kind, PublicKey, ReqTarget

if TYPE_CHECKING:
    from nostr_sdk import Client

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600.0
FETCH_TIMEOUT = timedelta(seconds=10)


@dataclass(frozen=True)
class Identity:
    """Subset of kind 0 metadata fields that bots commonly use."""

    pubkey_hex: str
    name: str | None = None
    display_name: str | None = None
    picture: str | None = None
    lud16: str | None = None
    nip05: str | None = None
    website: str | None = None
    about: str | None = None

    @property
    def best_name(self) -> str:
        """Most-readable name: display_name -> name -> shortened pubkey."""
        return self.display_name or self.name or f"{self.pubkey_hex[:8]}..."


@dataclass(frozen=True)
class _CacheEntry:
    identity: Identity
    timestamp: float


class IdentityResolver:
    """Fetch and cache kind 0 metadata per pubkey.

    Always returns an Identity (never None). Empty profiles are represented
    by an Identity with only `pubkey_hex` set. Both empty and populated
    results are cached for `ttl_seconds`.
    """

    def __init__(
        self,
        client: Client,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        fetch_timeout: timedelta = FETCH_TIMEOUT,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._fetch_timeout = fetch_timeout
        self._cache: dict[str, _CacheEntry] = {}

    async def resolve(self, pubkey_hex: str) -> Identity:
        """Return the Identity for pubkey_hex.

        Cache hit: returns immediately. Miss: fetches kind 0 via the client,
        parses, caches, returns. A fetch error returns the stale cached
        Identity if one exists, else an Identity with only pubkey_hex set;
        errors are NOT cached, so the next call retries.
        """
        now = time.monotonic()
        entry = self._cache.get(pubkey_hex)
        if entry is not None and (now - entry.timestamp) < self._ttl:
            return entry.identity

        try:
            identity = await self._fetch(pubkey_hex)
        except Exception:
            log.debug(
                "Failed to resolve identity for %s; not caching",
                pubkey_hex[:16], exc_info=True,
            )
            if entry is not None:
                return entry.identity
            return Identity(pubkey_hex=pubkey_hex)
        self._cache[pubkey_hex] = _CacheEntry(identity, now)
        return identity

    async def _fetch(self, pubkey_hex: str) -> Identity:
        """Fetch and parse kind 0. Raises on fetch/parse errors."""
        pk = PublicKey.parse(pubkey_hex)
        # nostr-sdk >=0.45 dropped Client.fetch_metadata; query kind 0 directly.
        f = Filter().kind(Kind(0)).author(pk).limit(1)
        events = await self._client.fetch_events(
            ReqTarget.auto([f]), self._fetch_timeout,
        )
        if events.is_empty():
            return Identity(pubkey_hex=pubkey_hex)
        data = json.loads(events.first().content())
        return Identity(
            pubkey_hex=pubkey_hex,
            name=_clean(data.get("name")),
            display_name=_clean(data.get("display_name")),
            picture=_clean(data.get("picture")),
            lud16=_clean(data.get("lud16")),
            nip05=_clean(data.get("nip05")),
            website=_clean(data.get("website")),
            about=_clean(data.get("about")),
        )

    def invalidate(self, pubkey_hex: str) -> None:
        """Drop the cached entry for `pubkey_hex`, forcing re-fetch next time."""
        self._cache.pop(pubkey_hex, None)

    def cleanup_expired(self) -> int:
        """Remove expired cache entries. Returns count removed."""
        now = time.monotonic()
        expired = [
            k for k, v in self._cache.items()
            if (now - v.timestamp) >= self._ttl
        ]
        for k in expired:
            del self._cache[k]
        return len(expired)


def _clean(value: object) -> str | None:
    """Treat empty strings, whitespace, and non-strings as None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
