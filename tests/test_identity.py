"""Tests for nostrbot_sdk.identity."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from nostrbot_sdk.identity import Identity, IdentityResolver

PUBKEY = "aa" * 32
OTHER = "bb" * 32


def _events_mock(data: dict | None) -> MagicMock:
    """Fake nostr_sdk.Events: empty when data is None, else one kind 0 event.

    Must be a MagicMock (not AsyncMock): is_empty/first/content are sync.
    """
    events = MagicMock()
    if data is None:
        events.is_empty.return_value = True
        events.first.return_value = None
        return events
    events.is_empty.return_value = False
    event = MagicMock()
    event.content.return_value = json.dumps(data)
    events.first.return_value = event
    return events


async def test_returns_full_identity_on_kind_0_hit() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({
        "name": "alice",
        "display_name": "Alice",
        "picture": "https://x/avatar.png",
        "lud16": "alice@unsaltedbutter.ai",
        "nip05": "alice@unsaltedbutter.ai",
        "website": "https://unsaltedbutter.ai",
        "about": "hi",
    })
    resolver = IdentityResolver(client)
    ident = await resolver.resolve(PUBKEY)
    assert ident.name == "alice"
    assert ident.display_name == "Alice"
    assert ident.lud16 == "alice@unsaltedbutter.ai"
    assert ident.nip05 == "alice@unsaltedbutter.ai"
    assert ident.about == "hi"
    assert ident.pubkey_hex == PUBKEY


async def test_returns_minimal_identity_when_no_kind_0() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock(None)
    resolver = IdentityResolver(client)
    ident = await resolver.resolve(PUBKEY)
    assert ident.pubkey_hex == PUBKEY
    assert ident.name is None
    assert ident.lud16 is None


async def test_returns_minimal_identity_on_fetch_error() -> None:
    client = AsyncMock()
    client.fetch_events.side_effect = RuntimeError("relay timeout")
    resolver = IdentityResolver(client)
    ident = await resolver.resolve(PUBKEY)
    assert ident.pubkey_hex == PUBKEY
    assert ident.name is None


async def test_caches_positive_result() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({"name": "alice"})
    resolver = IdentityResolver(client)
    await resolver.resolve(PUBKEY)
    await resolver.resolve(PUBKEY)
    client.fetch_events.assert_awaited_once()


async def test_caches_negative_result() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock(None)
    resolver = IdentityResolver(client)
    await resolver.resolve(PUBKEY)
    await resolver.resolve(PUBKEY)
    client.fetch_events.assert_awaited_once()


async def test_cache_expires_after_ttl() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({"name": "alice"})
    resolver = IdentityResolver(client, ttl_seconds=0.01)
    await resolver.resolve(PUBKEY)
    await asyncio.sleep(0.02)
    await resolver.resolve(PUBKEY)
    assert client.fetch_events.await_count == 2


async def test_different_pubkeys_cached_separately() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({"name": "x"})
    resolver = IdentityResolver(client)
    await resolver.resolve(PUBKEY)
    await resolver.resolve(OTHER)
    assert client.fetch_events.await_count == 2


async def test_invalidate_forces_refetch() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({"name": "alice"})
    resolver = IdentityResolver(client)
    await resolver.resolve(PUBKEY)
    resolver.invalidate(PUBKEY)
    await resolver.resolve(PUBKEY)
    assert client.fetch_events.await_count == 2


async def test_cleanup_expired_removes_old_entries() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock(None)
    resolver = IdentityResolver(client, ttl_seconds=0.01)
    await resolver.resolve(PUBKEY)
    assert len(resolver._cache) == 1
    await asyncio.sleep(0.02)
    removed = resolver.cleanup_expired()
    assert removed == 1
    assert len(resolver._cache) == 0


async def test_empty_string_fields_become_none() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({
        "name": "",
        "display_name": "   ",
        "lud16": "alice@unsaltedbutter.ai",
    })
    resolver = IdentityResolver(client)
    ident = await resolver.resolve(PUBKEY)
    assert ident.name is None
    assert ident.display_name is None
    assert ident.lud16 == "alice@unsaltedbutter.ai"


async def test_non_string_fields_become_none() -> None:
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({"name": 42, "lud16": None})
    resolver = IdentityResolver(client)
    ident = await resolver.resolve(PUBKEY)
    assert ident.name is None
    assert ident.lud16 is None


def test_best_name_prefers_display_name() -> None:
    i = Identity(pubkey_hex=PUBKEY, name="alice", display_name="Alice")
    assert i.best_name == "Alice"


def test_best_name_falls_back_to_name() -> None:
    i = Identity(pubkey_hex=PUBKEY, name="alice")
    assert i.best_name == "alice"


def test_best_name_falls_back_to_short_pubkey() -> None:
    i = Identity(pubkey_hex=PUBKEY)
    assert i.best_name == f"{PUBKEY[:8]}..."


async def test_fetch_error_not_cached_retries_next_call() -> None:
    """A fetch failure returns a minimal Identity but must not be cached:
    the next resolve() should retry the network."""
    client = AsyncMock()
    client.fetch_events.side_effect = RuntimeError("relay timeout")
    resolver = IdentityResolver(client)

    ident = await resolver.resolve(PUBKEY)
    assert ident.name is None

    client.fetch_events.side_effect = None
    client.fetch_events.return_value = _events_mock({"name": "alice"})
    ident = await resolver.resolve(PUBKEY)
    assert ident.name == "alice"
    assert client.fetch_events.await_count == 2


async def test_fetch_error_serves_stale_entry() -> None:
    """An expired cached Identity is served when the refresh fetch fails."""
    client = AsyncMock()
    client.fetch_events.return_value = _events_mock({"name": "alice"})
    resolver = IdentityResolver(client, ttl_seconds=0.0)  # everything expires

    assert (await resolver.resolve(PUBKEY)).name == "alice"

    client.fetch_events.side_effect = RuntimeError("relay down")
    assert (await resolver.resolve(PUBKEY)).name == "alice"
