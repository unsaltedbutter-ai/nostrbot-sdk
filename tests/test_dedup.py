"""Tests for nostrbot_sdk.dedup."""

from __future__ import annotations

import time

from nostrbot_sdk.dedup import Dedup


def test_check_and_add_returns_false_first_then_true() -> None:
    d: Dedup[str] = Dedup(ttl_seconds=60)
    assert d.check_and_add("abc") is False
    assert d.check_and_add("abc") is True
    assert d.check_and_add("abc") is True


def test_different_keys_are_independent() -> None:
    d: Dedup[str] = Dedup(ttl_seconds=60)
    assert d.check_and_add("a") is False
    assert d.check_and_add("b") is False
    assert d.check_and_add("a") is True
    assert d.check_and_add("b") is True


def test_works_with_tuple_keys() -> None:
    d: Dedup[tuple[str, str]] = Dedup(ttl_seconds=60)
    assert d.check_and_add(("alice", "hi")) is False
    assert d.check_and_add(("alice", "hi")) is True
    assert d.check_and_add(("alice", "bye")) is False
    assert d.check_and_add(("bob", "hi")) is False


def test_forget_removes_key() -> None:
    d: Dedup[str] = Dedup(ttl_seconds=60)
    d.check_and_add("abc")
    assert "abc" in d
    d.forget("abc")
    assert "abc" not in d
    assert d.check_and_add("abc") is False  # acts as new


def test_forget_missing_key_is_noop() -> None:
    d: Dedup[str] = Dedup(ttl_seconds=60)
    d.forget("nonexistent")  # should not raise


def test_expired_entries_pruned_after_prune_every_calls() -> None:
    """Pruning happens every N calls; entries older than TTL are dropped."""
    d: Dedup[str] = Dedup(ttl_seconds=0.01, prune_every=3)
    d.check_and_add("old")
    time.sleep(0.02)
    # Old is still in the dict because we haven't hit prune_every yet
    assert "old" in d
    # Three calls (one already happened above as check_and_add) should trigger prune
    d.check_and_add("new1")
    d.check_and_add("new2")
    # At this point prune should have run; "old" should be gone
    assert "old" not in d
    assert "new1" in d


def test_expired_key_returns_false_again_after_prune() -> None:
    """A key that expired and was pruned can be re-added as new."""
    d: Dedup[str] = Dedup(ttl_seconds=0.01, prune_every=2)
    d.check_and_add("abc")
    time.sleep(0.02)
    d.check_and_add("trigger_prune")  # second call triggers prune
    assert d.check_and_add("abc") is False  # treated as new


def test_len_reflects_active_entries() -> None:
    d: Dedup[str] = Dedup(ttl_seconds=60)
    assert len(d) == 0
    d.check_and_add("a")
    assert len(d) == 1
    d.check_and_add("b")
    assert len(d) == 2
    d.check_and_add("a")
    assert len(d) == 2  # duplicate doesn't grow
    d.forget("a")
    assert len(d) == 1


def test_prune_every_default_does_not_misbehave() -> None:
    """Even with default prune_every=50, fewer-than-50 calls should still dedup."""
    d: Dedup[str] = Dedup(ttl_seconds=60)
    for i in range(10):
        assert d.check_and_add(f"key{i}") is False
    for i in range(10):
        assert d.check_and_add(f"key{i}") is True
