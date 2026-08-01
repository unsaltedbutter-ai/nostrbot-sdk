"""NIP-40 expiration tag helpers.

NIP-40 lets the sender mark an event with an expiration timestamp; well-behaved
relays drop expired events on retrieval. We use this on outbound DMs so that
messages don't linger on relays forever.

For gift-wrapped (NIP-17) DMs the tag has to go in two places to be useful:

  * inside the rumor, so the recipient's client knows when the message expires;
  * on the outer gift wrap, so the relay can garbage-collect it. A relay only
    ever sees the wrap -- it cannot read the rumor -- so a tag that exists only
    on the inside is invisible to it and nothing is ever collected.

`dm_expiration_tag` produces a single tag intended to be stamped on both, so
the two agree exactly. See its docstring for why the timestamp is not simply
`now + seconds_from_now`.
"""

from __future__ import annotations

import logging
import secrets

from nostr_sdk import Tag, Timestamp

log = logging.getLogger(__name__)

# Default expiration: 7 days
DEFAULT_DM_EXPIRY_SECS = 7 * 24 * 3600

# NIP-59 randomizes a gift wrap's `created_at` up to two days into the past so
# observers can't tell when the message was really sent.
GIFT_WRAP_MAX_BACKDATE_SECS = 2 * 24 * 3600

# Cap our own backdating at a quarter of the expiry so a short-lived DM keeps
# most of its configured lifetime (and can never be born already expired).
_BACKDATE_FRACTION = 4


def expiration_tag(seconds_from_now: int = DEFAULT_DM_EXPIRY_SECS) -> Tag:
    """Return a NIP-40 expiration tag set N seconds from now (default 7 days).

    Absolute wall-clock expiry, suitable for events whose `created_at` is the
    real send time (kind 1 notes, kind 4 DMs, long-form articles). For NIP-17
    gift wraps use `dm_expiration_tag` instead.
    """
    return Tag.expiration(
        Timestamp.from_secs(Timestamp.now().as_secs() + seconds_from_now)
    )


def dm_expiration_tag(seconds_from_now: int = DEFAULT_DM_EXPIRY_SECS) -> Tag:
    """Return one NIP-40 tag to stamp on BOTH the rumor and its gift wrap.

    The timestamp is anchored to a randomly backdated point rather than to
    `now`. That matters: a gift wrap's `created_at` is deliberately randomized
    into the past, so an absolute `now + N` expiration would hand any observer
    the real send time back as `expiration - N` (N is a small, guessable set of
    values -- 7 days by default) and undo the randomization NIP-59 exists to
    provide. Anchoring to `now - r` for a secret r keeps the send time hidden
    while still letting the identical tag go inside and outside the wrap.

    The backdate r is drawn from `[0, min(2 days, seconds_from_now / 4)]`, so
    at least three quarters of the configured lifetime always survives and the
    wrap is never created in an already-expired state. With the 7-day default
    that yields an effective lifetime of 5 to 7 days.

    Logs a warning when `seconds_from_now` is at or below the 2-day gift-wrap
    randomization window: such DMs get proportionally less timing privacy, and
    a recipient who is offline briefly may never see them.
    """
    if seconds_from_now <= GIFT_WRAP_MAX_BACKDATE_SECS:
        log.warning(
            "dm expiry of %ds is at or below the %ds NIP-59 gift-wrap "
            "randomization window; timing privacy is reduced and relays may "
            "drop the wrap before an offline recipient fetches it",
            seconds_from_now, GIFT_WRAP_MAX_BACKDATE_SECS,
        )

    max_backdate = min(
        GIFT_WRAP_MAX_BACKDATE_SECS, seconds_from_now // _BACKDATE_FRACTION,
    )
    # randbelow(0) raises; a non-positive bound just means "don't backdate".
    backdate = secrets.randbelow(max_backdate + 1) if max_backdate > 0 else 0
    anchor = Timestamp.now().as_secs() - backdate
    return Tag.expiration(Timestamp.from_secs(anchor + seconds_from_now))
