"""Tests for nostrbot_sdk.expiration: NIP-40 tags for plain events and DMs."""

from __future__ import annotations

import logging
import time

from nostrbot_sdk.expiration import (
    DEFAULT_DM_EXPIRY_SECS,
    GIFT_WRAP_MAX_BACKDATE_SECS,
    dm_expiration_tag,
    expiration_tag,
)

LOGGER = "nostrbot_sdk.expiration"


def _secs(tag) -> int:
    vec = tag.as_vec()
    assert vec[0] == "expiration"
    return int(vec[1])


# -- expiration_tag: plain absolute expiry -------------------------------------


def test_expiration_tag_is_absolute_now_plus_n() -> None:
    now = int(time.time())
    assert _secs(expiration_tag(3600)) - (now + 3600) <= 2


def test_expiration_tag_defaults_to_seven_days() -> None:
    now = int(time.time())
    assert _secs(expiration_tag()) - (now + DEFAULT_DM_EXPIRY_SECS) <= 2


# -- dm_expiration_tag: backdated, shared between rumor and wrap ---------------


def test_dm_expiration_tag_never_exceeds_now_plus_n() -> None:
    """Backdating only ever moves the anchor into the past."""
    now = int(time.time())
    for _ in range(50):
        assert _secs(dm_expiration_tag(DEFAULT_DM_EXPIRY_SECS)) <= now + DEFAULT_DM_EXPIRY_SECS + 2


def test_dm_expiration_tag_keeps_at_least_three_quarters_of_lifetime() -> None:
    """A DM must not lose most of its configured lifetime to randomization."""
    now = int(time.time())
    floor = now + DEFAULT_DM_EXPIRY_SECS - GIFT_WRAP_MAX_BACKDATE_SECS
    for _ in range(50):
        assert _secs(dm_expiration_tag(DEFAULT_DM_EXPIRY_SECS)) >= floor - 2


def test_dm_expiration_tag_is_never_born_expired() -> None:
    """Our own backdating must not push the expiry into the past, or relays
    would drop the wrap the moment it arrives."""
    now = int(time.time())
    for seconds in (1, 60, 3600, 86400, DEFAULT_DM_EXPIRY_SECS, 30 * 86400):
        for _ in range(20):
            assert _secs(dm_expiration_tag(seconds)) >= now


def test_dm_expiration_tag_actually_randomizes() -> None:
    """A constant anchor would make expiration - N a perfect send-time oracle."""
    values = {_secs(dm_expiration_tag(DEFAULT_DM_EXPIRY_SECS)) for _ in range(50)}
    assert len(values) > 1


def test_dm_expiration_tag_hides_send_time_for_default_expiry() -> None:
    """An observer subtracting the (publicly documented) default expiry from
    the tag should land before the real send time, not on it."""
    now = int(time.time())
    implied = [
        _secs(dm_expiration_tag(DEFAULT_DM_EXPIRY_SECS)) - DEFAULT_DM_EXPIRY_SECS
        for _ in range(50)
    ]
    assert all(t <= now for t in implied)
    assert min(implied) < now  # at least some samples are genuinely backdated


def test_dm_expiration_tag_tiny_expiry_does_not_raise() -> None:
    """seconds_from_now // 4 == 0 must not hit secrets.randbelow(0)."""
    now = int(time.time())
    assert _secs(dm_expiration_tag(1)) >= now


# -- dm_expiration_tag: short-expiry warning -----------------------------------


def test_dm_expiration_tag_warns_below_gift_wrap_window(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        dm_expiration_tag(3600)
    assert any("randomization window" in r.getMessage() for r in caplog.records)


def test_dm_expiration_tag_warns_at_exactly_the_window(caplog) -> None:
    """At exactly 2 days the backdate can equal the lifetime, so still warn."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        dm_expiration_tag(GIFT_WRAP_MAX_BACKDATE_SECS)
    assert caplog.records


def test_dm_expiration_tag_silent_for_default(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        dm_expiration_tag(DEFAULT_DM_EXPIRY_SECS)
    assert not caplog.records
