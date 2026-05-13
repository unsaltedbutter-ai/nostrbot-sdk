"""NIP-17 DM relay list (kind 10050) detection and caching.

Checks whether a recipient has published a kind 10050 event, which
indicates they support NIP-17 private DMs and lists their preferred
DM inbox relays. Results are cached with TTL to avoid repeated relay
queries.

Usage:
    detector = Nip17Support(client, ttl_seconds=3600)
    result = await detector.check(recipient_pubkey_hex)
    if result is not None:
        # User supports NIP-17; result is a list of relay URL strings
        await client.send_private_msg_to(
            [RelayUrl.parse(r) for r in result], pk, message, []
        )
    else:
        # Fall back to NIP-04
        ...
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from nostr_sdk import Filter, Kind, PublicKey

if TYPE_CHECKING:
    from nostr_sdk import Client

log = logging.getLogger(__name__)

# Kind 10050: NIP-17 DM relay list (replaceable event)
KIND_DM_RELAYS = Kind(10050)

# Default cache TTL: 1 hour
DEFAULT_TTL_SECONDS = 3600.0

# Timeout for relay queries
FETCH_TIMEOUT = timedelta(seconds=10)


@dataclass(frozen=True)
class _CacheEntry:
    """Cached result of a kind 10050 lookup."""

    relay_urls: list[str] | None  # None = no kind 10050 found
    timestamp: float              # monotonic time of the lookup


class Nip17Support:
    """Detect and cache whether recipients support NIP-17 DMs.

    Queries connected relays for the recipient's kind 10050 event.
    If found, extracts the relay URLs from "relay" tags. Results are
    cached (both positive and negative) to avoid repeated network queries.

    Thread-safe for concurrent asyncio tasks (dict operations are atomic
    in CPython, and we tolerate benign races on cache writes).
    """

    def __init__(
        self,
        client: Client,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}

    async def check(self, pubkey_hex: str) -> list[str] | None:
        """Check if a pubkey has a kind 10050 event (NIP-17 DM relay list).

        Returns a list of relay URL strings if the user supports NIP-17,
        or None if they don't (no kind 10050 published).

        Results are cached for ttl_seconds (both positive and negative).
        """
        now = time.monotonic()

        # Check cache first
        entry = self._cache.get(pubkey_hex)
        if entry is not None and (now - entry.timestamp) < self._ttl:
            return entry.relay_urls

        # Cache miss or expired: query relays
        relay_urls = await self._fetch_dm_relays(pubkey_hex)

        # Cache the result (even if None, to avoid repeated lookups)
        self._cache[pubkey_hex] = _CacheEntry(
            relay_urls=relay_urls,
            timestamp=now,
        )

        if relay_urls:
            log.info(
                "NIP-17 supported by %s (%d inbox relay(s))",
                pubkey_hex[:16], len(relay_urls),
            )
        else:
            log.debug("No kind 10050 for %s, will use NIP-04", pubkey_hex[:16])

        return relay_urls

    async def _fetch_dm_relays(self, pubkey_hex: str) -> list[str] | None:
        """Query connected relays for the recipient's kind 10050 event.

        Returns a list of relay URLs from the event's "relay" tags,
        or None if no kind 10050 event is found.
        """
        try:
            pk = PublicKey.parse(pubkey_hex)
            f = Filter().kind(KIND_DM_RELAYS).author(pk).limit(1)
            events = await self._client.fetch_events(f, FETCH_TIMEOUT)

            if events.is_empty():
                return None

            event = events.first()
            relay_urls: list[str] = []
            for tag in event.tags().to_vec():
                tag_vec = tag.as_vec()
                if len(tag_vec) >= 2 and tag_vec[0] == "relay":
                    url = tag_vec[1].strip()
                    if url:
                        relay_urls.append(url)

            if not relay_urls:
                # Kind 10050 exists but has no relay tags: treat as unsupported
                log.debug(
                    "Kind 10050 for %s has no relay tags", pubkey_hex[:16],
                )
                return None

            return relay_urls

        except Exception:
            log.debug(
                "Failed to fetch kind 10050 for %s, falling back to NIP-04",
                pubkey_hex[:16],
                exc_info=True,
            )
            return None

    def invalidate(self, pubkey_hex: str) -> None:
        """Remove a cached entry (e.g., when a user changes their relay list)."""
        self._cache.pop(pubkey_hex, None)

    def cleanup_expired(self) -> int:
        """Remove expired cache entries. Returns count of entries removed."""
        now = time.monotonic()
        expired = [
            k for k, v in self._cache.items()
            if (now - v.timestamp) >= self._ttl
        ]
        for k in expired:
            del self._cache[k]
        return len(expired)
