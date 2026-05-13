"""Tests for nostrbot_sdk.locks."""

from __future__ import annotations

import asyncio

from nostrbot_sdk.locks import UserLockManager


def test_get_returns_same_lock_for_same_key() -> None:
    m = UserLockManager()
    lock1 = m.get("alice")
    lock2 = m.get("alice")
    assert lock1 is lock2


def test_get_returns_different_locks_for_different_keys() -> None:
    m = UserLockManager()
    assert m.get("alice") is not m.get("bob")


def test_len_tracks_unique_keys() -> None:
    m = UserLockManager()
    assert len(m) == 0
    m.get("a")
    assert len(m) == 1
    m.get("b")
    assert len(m) == 2
    m.get("a")
    assert len(m) == 2


def test_contains() -> None:
    m = UserLockManager()
    assert "alice" not in m
    m.get("alice")
    assert "alice" in m


async def test_lock_actually_serializes() -> None:
    """Two coroutines using the same key's lock should run sequentially."""
    m = UserLockManager()
    order: list[str] = []

    async def task(name: str, delay: float) -> None:
        async with m.get("shared"):
            order.append(f"{name}:start")
            await asyncio.sleep(delay)
            order.append(f"{name}:end")

    # Start B first (with short sleep). It should acquire the lock,
    # then A will queue behind it.
    await asyncio.gather(task("B", 0.02), task("A", 0.01))
    # First task to start owns the lock through completion
    assert order[0].endswith(":start")
    assert order[1] == order[0].replace(":start", ":end")


async def test_cleanup_idle_removes_idle_unheld_locks() -> None:
    m = UserLockManager(idle_seconds=0.01)
    m.get("alice")
    m.get("bob")
    assert len(m) == 2
    await asyncio.sleep(0.02)
    removed = m.cleanup_idle()
    assert removed == 2
    assert len(m) == 0


async def test_cleanup_preserves_held_locks_even_if_idle() -> None:
    """A held lock with a stale last-use time should NOT be cleaned up."""
    m = UserLockManager(idle_seconds=0.01)
    lock = m.get("alice")
    await lock.acquire()
    try:
        await asyncio.sleep(0.02)  # Make it look idle
        removed = m.cleanup_idle()
        assert removed == 0
        assert "alice" in m
    finally:
        lock.release()


async def test_cleanup_preserves_recently_used() -> None:
    m = UserLockManager(idle_seconds=60)
    m.get("alice")
    removed = m.cleanup_idle()
    assert removed == 0
    assert "alice" in m


def test_cleanup_on_empty_manager_is_safe() -> None:
    m = UserLockManager()
    assert m.cleanup_idle() == 0


async def test_get_after_cleanup_recreates_lock() -> None:
    m = UserLockManager(idle_seconds=0.01)
    first = m.get("alice")
    await asyncio.sleep(0.02)
    m.cleanup_idle()
    assert "alice" not in m
    second = m.get("alice")
    assert first is not second
