"""Tests for nostrbot_sdk.nip17_support: kind 10050 detection and caching."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nostrbot_sdk.nip17_support import Nip17Support


# -- Helpers -------------------------------------------------------------------


def _make_events_result(events_list: list | None = None):
    """Build a mock Events result from client.fetch_events.

    If events_list is None, returns an empty result (is_empty() == True).
    If events_list is provided, returns a result where first() is events_list[0].
    """
    result = MagicMock()
    if events_list is None or len(events_list) == 0:
        result.is_empty.return_value = True
        result.first.return_value = None
    else:
        result.is_empty.return_value = False
        result.first.return_value = events_list[0]
    return result


def _make_event_with_relay_tags(relay_urls: list[str]):
    """Build a mock Event with relay tags (kind 10050 format)."""
    event = MagicMock()
    tags = []
    for url in relay_urls:
        tag = MagicMock()
        tag.as_vec.return_value = ["relay", url]
        tags.append(tag)
    tag_list = MagicMock()
    tag_list.to_vec.return_value = tags
    event.tags.return_value = tag_list
    return event


def _make_event_with_no_relay_tags():
    """Build a mock Event with no relay tags (malformed kind 10050)."""
    event = MagicMock()
    tag_list = MagicMock()
    tag_list.to_vec.return_value = []
    event.tags.return_value = tag_list
    return event


PUBKEY_HEX = "aa" * 32


# -- Tests: basic detection ---------------------------------------------------


class TestNip17SupportCheck:
    """Test the check() method for kind 10050 detection."""

    async def test_returns_relay_urls_when_kind_10050_found(self) -> None:
        """When a kind 10050 event with relay tags exists, returns the relay URLs."""
        client = AsyncMock()
        event = _make_event_with_relay_tags([
            "wss://relay1.example.com",
            "wss://relay2.example.com",
        ])
        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)
        result = await detector.check(PUBKEY_HEX)

        assert result == [
            "wss://relay1.example.com",
            "wss://relay2.example.com",
        ]

    async def test_returns_none_when_no_kind_10050(self) -> None:
        """When no kind 10050 event exists, returns None."""
        client = AsyncMock()
        client.fetch_events.return_value = _make_events_result(None)

        detector = Nip17Support(client)
        result = await detector.check(PUBKEY_HEX)

        assert result is None

    async def test_returns_none_when_kind_10050_has_no_relay_tags(self) -> None:
        """When kind 10050 exists but has no relay tags, returns None."""
        client = AsyncMock()
        event = _make_event_with_no_relay_tags()
        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)
        result = await detector.check(PUBKEY_HEX)

        assert result is None

    async def test_returns_none_on_fetch_error(self) -> None:
        """When fetch_events raises an exception, returns None (graceful fallback)."""
        client = AsyncMock()
        client.fetch_events.side_effect = RuntimeError("relay down")

        detector = Nip17Support(client)
        result = await detector.check(PUBKEY_HEX)

        assert result is None

    async def test_strips_whitespace_from_relay_urls(self) -> None:
        """Relay URLs with whitespace are stripped."""
        client = AsyncMock()
        event = _make_event_with_relay_tags([" wss://relay.example.com "])
        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)
        result = await detector.check(PUBKEY_HEX)

        assert result == ["wss://relay.example.com"]

    async def test_skips_empty_relay_urls(self) -> None:
        """Empty relay URL strings are filtered out."""
        client = AsyncMock()
        event = MagicMock()
        tags = []
        for url in ["wss://valid.com", "", "  "]:
            tag = MagicMock()
            tag.as_vec.return_value = ["relay", url]
            tags.append(tag)
        tag_list = MagicMock()
        tag_list.to_vec.return_value = tags
        event.tags.return_value = tag_list

        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)
        result = await detector.check(PUBKEY_HEX)

        assert result == ["wss://valid.com"]

    async def test_ignores_non_relay_tags(self) -> None:
        """Tags that are not 'relay' tags are ignored."""
        client = AsyncMock()
        event = MagicMock()
        tags = []
        t1 = MagicMock()
        t1.as_vec.return_value = ["relay", "wss://inbox.example.com"]
        tags.append(t1)
        t2 = MagicMock()
        t2.as_vec.return_value = ["p", "aa" * 32]
        tags.append(t2)
        t3 = MagicMock()
        t3.as_vec.return_value = ["relay"]
        tags.append(t3)
        tag_list = MagicMock()
        tag_list.to_vec.return_value = tags
        event.tags.return_value = tag_list

        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)
        result = await detector.check(PUBKEY_HEX)

        assert result == ["wss://inbox.example.com"]


# -- Tests: caching -----------------------------------------------------------


class TestNip17SupportCache:
    """Test the caching behavior of check()."""

    async def test_positive_result_cached(self) -> None:
        """A positive result (relays found) is cached on second call."""
        client = AsyncMock()
        event = _make_event_with_relay_tags(["wss://relay.example.com"])
        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)

        result1 = await detector.check(PUBKEY_HEX)
        result2 = await detector.check(PUBKEY_HEX)

        assert result1 == ["wss://relay.example.com"]
        assert result2 == ["wss://relay.example.com"]
        client.fetch_events.assert_awaited_once()

    async def test_negative_result_cached(self) -> None:
        """A negative result (no kind 10050) is also cached."""
        client = AsyncMock()
        client.fetch_events.return_value = _make_events_result(None)

        detector = Nip17Support(client)

        result1 = await detector.check(PUBKEY_HEX)
        result2 = await detector.check(PUBKEY_HEX)

        assert result1 is None
        assert result2 is None
        client.fetch_events.assert_awaited_once()

    async def test_cache_expires_after_ttl(self) -> None:
        """Cached entries expire after the TTL and are re-fetched."""
        client = AsyncMock()
        event = _make_event_with_relay_tags(["wss://relay.example.com"])
        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client, ttl_seconds=0.01)

        result1 = await detector.check(PUBKEY_HEX)
        assert result1 == ["wss://relay.example.com"]
        assert client.fetch_events.await_count == 1

        import asyncio
        await asyncio.sleep(0.02)

        result2 = await detector.check(PUBKEY_HEX)
        assert result2 == ["wss://relay.example.com"]
        assert client.fetch_events.await_count == 2

    async def test_different_pubkeys_cached_separately(self) -> None:
        """Each pubkey has its own cache entry."""
        other_pubkey = "bb" * 32

        client = AsyncMock()
        event = _make_event_with_relay_tags(["wss://relay.example.com"])
        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)

        await detector.check(PUBKEY_HEX)
        await detector.check(other_pubkey)

        assert client.fetch_events.await_count == 2

    async def test_invalidate_removes_cache_entry(self) -> None:
        """invalidate() removes a cached entry, forcing re-fetch on next check."""
        client = AsyncMock()
        event = _make_event_with_relay_tags(["wss://relay.example.com"])
        client.fetch_events.return_value = _make_events_result([event])

        detector = Nip17Support(client)

        await detector.check(PUBKEY_HEX)
        assert client.fetch_events.await_count == 1

        detector.invalidate(PUBKEY_HEX)

        await detector.check(PUBKEY_HEX)
        assert client.fetch_events.await_count == 2

    async def test_invalidate_nonexistent_key_is_noop(self) -> None:
        """invalidate() on a key that doesn't exist is a safe no-op."""
        client = AsyncMock()
        detector = Nip17Support(client)
        detector.invalidate("nonexistent")  # Should not raise


# -- Tests: cleanup -----------------------------------------------------------


class TestNip17SupportCleanup:
    """Test the cleanup_expired() method."""

    async def test_cleanup_removes_expired_entries(self) -> None:
        """cleanup_expired() removes entries older than TTL."""
        client = AsyncMock()
        client.fetch_events.return_value = _make_events_result(None)

        detector = Nip17Support(client, ttl_seconds=0.01)

        await detector.check(PUBKEY_HEX)
        assert len(detector._cache) == 1

        import asyncio
        await asyncio.sleep(0.02)

        removed = detector.cleanup_expired()
        assert removed == 1
        assert len(detector._cache) == 0

    async def test_cleanup_preserves_fresh_entries(self) -> None:
        """cleanup_expired() preserves entries that haven't expired."""
        client = AsyncMock()
        client.fetch_events.return_value = _make_events_result(None)

        detector = Nip17Support(client, ttl_seconds=3600)

        await detector.check(PUBKEY_HEX)
        assert len(detector._cache) == 1

        removed = detector.cleanup_expired()
        assert removed == 0
        assert len(detector._cache) == 1
