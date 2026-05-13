"""NIP-40 expiration tag helper.

NIP-40 lets the sender mark an event with an expiration timestamp; well-behaved
relays drop expired events on retrieval. We use this on outbound DMs so that
messages don't linger on relays forever.
"""

from __future__ import annotations

from nostr_sdk import Tag, Timestamp

# Default expiration: 7 days
DEFAULT_DM_EXPIRY_SECS = 7 * 24 * 3600


def expiration_tag(seconds_from_now: int = DEFAULT_DM_EXPIRY_SECS) -> Tag:
    """Return a NIP-40 expiration tag set N seconds from now (default 7 days)."""
    return Tag.expiration(
        Timestamp.from_secs(Timestamp.now().as_secs() + seconds_from_now)
    )
