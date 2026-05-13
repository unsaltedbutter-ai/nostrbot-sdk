"""Tests for nostrbot_sdk.context."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from nostrbot_sdk.context import SenderContext


async def test_reply_calls_bot_send_dm_with_sender_hex() -> None:
    bot = MagicMock()
    bot.send_dm = AsyncMock()
    ctx = SenderContext("aa" * 32, "npub1xyz", "nip17", _bot=bot)
    await ctx.reply("hello")
    bot.send_dm.assert_awaited_once_with("aa" * 32, "hello")


async def test_reply_works_for_each_protocol() -> None:
    bot = MagicMock()
    bot.send_dm = AsyncMock()
    for proto in ("nip04", "nip17", "zap"):
        ctx = SenderContext("bb" * 32, "npub1xyz", proto, _bot=bot)  # type: ignore[arg-type]
        await ctx.reply(f"hi {proto}")
    assert bot.send_dm.await_count == 3


async def test_resolve_identity_delegates_to_bot_identity_resolver() -> None:
    bot = MagicMock()
    fake_identity = object()
    bot.identity.resolve = AsyncMock(return_value=fake_identity)
    ctx = SenderContext("cc" * 32, "npub1xyz", "nip04", _bot=bot)
    result = await ctx.resolve_identity()
    bot.identity.resolve.assert_awaited_once_with("cc" * 32)
    assert result is fake_identity


def test_sender_context_holds_all_fields() -> None:
    bot = MagicMock()
    ctx = SenderContext("dd" * 32, "npub1abc", "nip04", _bot=bot)
    assert ctx.sender_hex == "dd" * 32
    assert ctx.sender_npub == "npub1abc"
    assert ctx.protocol == "nip04"
