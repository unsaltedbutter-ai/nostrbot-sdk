"""Tests for nostrbot_sdk.publishing: PublishResult, tag builders, send helpers, Publisher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nostr_sdk import EventId, Keys, Tag

from nostrbot_sdk.publishing import (
    PublishResult,
    Publisher,
    build_article_tags,
    build_note_tags,
    normalize_hashtag,
    send_article,
    send_note,
)


# -- Helpers -------------------------------------------------------------------


def _tag_vec(tag: Tag) -> list[str]:
    return list(tag.as_vec())


def _tag_kinds(tags: list[Tag]) -> list[str]:
    """First element of each tag (`e`, `p`, `q`, etc.)."""
    return [t.as_vec()[0] for t in tags]


def _output_mock(
    *,
    event_id_hex: str = "ab" * 32,
    success: list[str] | None = None,
    failed: dict[str, str] | None = None,
) -> MagicMock:
    """Mock a SendEventOutput."""
    out = MagicMock()
    eid = MagicMock()
    eid.to_hex.return_value = event_id_hex
    eid.to_bech32.return_value = "note1" + "x" * 58
    out.id = eid
    # `success` and `failed` keys are RelayUrl instances in real code; str()
    # converts them. Our mocks just need to be string-coercible.
    out.success = [MagicMock(__str__=lambda self, r=r: r) for r in (success or [])]
    failed = failed or {}
    out.failed = {
        MagicMock(__str__=lambda self, r=r: r): err for r, err in failed.items()
    }
    return out


# -- normalize_hashtag --------------------------------------------------------


def test_normalize_hashtag_strips_hash_and_lowercases() -> None:
    assert normalize_hashtag("#Nostr") == "nostr"
    assert normalize_hashtag("Bitcoin") == "bitcoin"
    assert normalize_hashtag("  #Lightning  ") == "lightning"


def test_normalize_hashtag_empty_returns_empty() -> None:
    assert normalize_hashtag("") == ""
    assert normalize_hashtag("#") == ""
    assert normalize_hashtag("   ") == ""


# -- build_note_tags: NIP-10 reply --------------------------------------------


class TestBuildNoteTagsReply:
    def test_direct_reply_emits_single_root_e_tag(self) -> None:
        parent = "aa" * 32
        tags = build_note_tags(reply_to=parent)
        # Only one e tag with "root" marker (NIP-10 marked mode, direct reply).
        e_tags = [t for t in tags if _tag_vec(t)[0] == "e"]
        assert len(e_tags) == 1
        v = _tag_vec(e_tags[0])
        assert v[1] == parent
        assert v[3] == "root"

    def test_thread_reply_emits_both_root_and_reply(self) -> None:
        root = "aa" * 32
        parent = "bb" * 32
        tags = build_note_tags(reply_to=parent, reply_root=root)
        e_tags = [t for t in tags if _tag_vec(t)[0] == "e"]
        assert len(e_tags) == 2
        markers = {_tag_vec(t)[3] for t in e_tags}
        assert markers == {"root", "reply"}
        # And the right id under each marker.
        by_marker = {_tag_vec(t)[3]: _tag_vec(t)[1] for t in e_tags}
        assert by_marker["root"] == root
        assert by_marker["reply"] == parent

    def test_no_reply_no_e_tags(self) -> None:
        tags = build_note_tags()
        assert not any(_tag_vec(t)[0] == "e" for t in tags)

    def test_reply_root_equals_reply_to_still_one_e_tag(self) -> None:
        """If caller passes reply_root == reply_to, treat as direct reply."""
        parent = "aa" * 32
        tags = build_note_tags(reply_to=parent, reply_root=parent)
        e_tags = [t for t in tags if _tag_vec(t)[0] == "e"]
        assert len(e_tags) == 1
        assert _tag_vec(e_tags[0])[3] == "root"


# -- build_note_tags: NIP-18 quote --------------------------------------------


class TestBuildNoteTagsQuote:
    def test_quote_with_author_emits_q_and_p(self) -> None:
        quoted = "aa" * 32
        author = Keys.generate().public_key().to_hex()
        tags = build_note_tags(quote=quoted, quote_author=author)
        kinds = _tag_kinds(tags)
        assert "q" in kinds
        assert "p" in kinds
        q_tag = next(t for t in tags if _tag_vec(t)[0] == "q")
        assert _tag_vec(q_tag)[1] == quoted
        # Author should be in a p-tag.
        p_tags = [t for t in tags if _tag_vec(t)[0] == "p"]
        assert any(_tag_vec(t)[1] == author for t in p_tags)

    def test_quote_without_author_emits_only_q_tag(self) -> None:
        quoted = "aa" * 32
        tags = build_note_tags(quote=quoted)
        assert _tag_kinds(tags).count("q") == 1
        assert _tag_kinds(tags).count("p") == 0

    def test_quote_author_dedup_against_mention_pubkeys(self) -> None:
        author = Keys.generate().public_key().to_hex()
        tags = build_note_tags(
            quote="aa" * 32,
            quote_author=author,
            mention_pubkeys=[author, Keys.generate().public_key().to_hex()],
        )
        p_tags = [t for t in tags if _tag_vec(t)[0] == "p"]
        # Quoted author appears exactly once even though mention_pubkeys repeats it.
        assert sum(1 for t in p_tags if _tag_vec(t)[1] == author) == 1
        # Plus the other mention.
        assert len(p_tags) == 2


# -- build_note_tags: hashtags ------------------------------------------------


class TestBuildNoteTagsHashtags:
    def test_normalizes_hashtags(self) -> None:
        tags = build_note_tags(hashtags=["#Nostr", "Bitcoin", "  #Lightning  "])
        t_tags = [_tag_vec(t)[1] for t in tags if _tag_vec(t)[0] == "t"]
        assert t_tags == ["nostr", "bitcoin", "lightning"]

    def test_drops_empty_hashtags(self) -> None:
        tags = build_note_tags(hashtags=["", "#", "  ", "nostr"])
        t_tags = [_tag_vec(t)[1] for t in tags if _tag_vec(t)[0] == "t"]
        assert t_tags == ["nostr"]

    def test_dedups_hashtags(self) -> None:
        tags = build_note_tags(hashtags=["nostr", "#Nostr", "NOSTR"])
        t_tags = [_tag_vec(t)[1] for t in tags if _tag_vec(t)[0] == "t"]
        assert t_tags == ["nostr"]


# -- build_note_tags: mentions, expiration, extras ----------------------------


def test_mention_pubkeys_become_p_tags() -> None:
    a = Keys.generate().public_key().to_hex()
    b = Keys.generate().public_key().to_hex()
    tags = build_note_tags(mention_pubkeys=[a, b])
    p_tags = [_tag_vec(t)[1] for t in tags if _tag_vec(t)[0] == "p"]
    assert set(p_tags) == {a, b}


def test_empty_mention_pubkeys_are_skipped() -> None:
    tags = build_note_tags(mention_pubkeys=["", Keys.generate().public_key().to_hex()])
    p_tags = [t for t in tags if _tag_vec(t)[0] == "p"]
    assert len(p_tags) == 1


def test_expiration_seconds_appends_expiration_tag() -> None:
    tags = build_note_tags(expiration_seconds=3600)
    assert any(_tag_vec(t)[0] == "expiration" for t in tags)


def test_extra_tags_appended_verbatim() -> None:
    custom = Tag.parse(["custom", "value"])
    tags = build_note_tags(extra_tags=[custom])
    assert any(_tag_vec(t) == ["custom", "value"] for t in tags)


# -- build_article_tags (NIP-23) ----------------------------------------------


class TestBuildArticleTags:
    def test_emits_required_d_and_title(self) -> None:
        tags = build_article_tags(identifier="article-1", title="Hello")
        by_kind = {_tag_vec(t)[0]: _tag_vec(t)[1] for t in tags}
        assert by_kind["d"] == "article-1"
        assert by_kind["title"] == "Hello"

    def test_optional_fields(self) -> None:
        tags = build_article_tags(
            identifier="x",
            title="t",
            summary="summary text",
            image="https://example.com/i.png",
            hashtags=["#Nostr", "Bitcoin"],
            published_at=1700000000,
        )
        by_kind = {_tag_vec(t)[0]: _tag_vec(t)[1:] for t in tags}
        assert by_kind["summary"] == ["summary text"]
        assert by_kind["image"] == ["https://example.com/i.png"]
        assert by_kind["published_at"] == ["1700000000"]
        t_tags = [_tag_vec(t)[1] for t in tags if _tag_vec(t)[0] == "t"]
        assert t_tags == ["nostr", "bitcoin"]

    def test_missing_identifier_raises(self) -> None:
        with pytest.raises(ValueError, match="identifier"):
            build_article_tags(identifier="", title="t")


# -- send_note: PublishResult shape -------------------------------------------


async def test_send_note_returns_publish_result_on_success() -> None:
    client = MagicMock()
    client.send_event_builder = AsyncMock(
        return_value=_output_mock(
            event_id_hex="cd" * 32,
            success=["wss://relay1/", "wss://relay2/"],
        ),
    )
    result = await send_note(client, "hello world")
    assert isinstance(result, PublishResult)
    assert result.event_id == "cd" * 32
    assert result.note_id.startswith("note1")
    assert set(result.success_relays) == {"wss://relay1/", "wss://relay2/"}
    assert result.failed_relays == {}
    assert result.kind == 1
    assert result.ok is True
    assert result.relay_count == 2


async def test_send_note_captures_failed_relays() -> None:
    client = MagicMock()
    client.send_event_builder = AsyncMock(
        return_value=_output_mock(
            success=["wss://relay1/"],
            failed={"wss://relay2/": "timeout"},
        ),
    )
    result = await send_note(client, "x")
    assert result.success_relays == ["wss://relay1/"]
    assert result.failed_relays == {"wss://relay2/": "timeout"}
    assert result.ok is True  # at least one relay accepted


async def test_send_note_zero_relays_means_not_ok() -> None:
    client = MagicMock()
    client.send_event_builder = AsyncMock(
        return_value=_output_mock(
            success=[],
            failed={"wss://r1/": "rejected", "wss://r2/": "auth required"},
        ),
    )
    result = await send_note(client, "x")
    assert result.ok is False
    assert result.relay_count == 0
    assert "wss://r1/" in result.failed_relays


async def test_send_note_passes_kwargs_through_tag_builder() -> None:
    client = MagicMock()
    client.send_event_builder = AsyncMock(return_value=_output_mock(success=["x"]))
    parent = "aa" * 32
    await send_note(client, "hi", reply_to=parent, hashtags=["nostr"])
    # send_event_builder was called with a builder; we can't easily inspect the
    # builder's internal tags via the public API, but the fact that send_note
    # completes proves the tag list was built without error. Tag content is
    # already covered by build_note_tags tests.
    client.send_event_builder.assert_awaited_once()


# -- send_article -------------------------------------------------------------


async def test_send_article_kind_is_30023() -> None:
    client = MagicMock()
    client.send_event_builder = AsyncMock(return_value=_output_mock(success=["x"]))
    result = await send_article(
        client, "My Article", "# content", identifier="my-article",
    )
    assert result.kind == 30023


async def test_send_article_requires_identifier() -> None:
    client = MagicMock()
    with pytest.raises(ValueError, match="identifier"):
        await send_article(client, "t", "c", identifier="")


# -- Publisher ----------------------------------------------------------------


class TestPublisher:
    def test_constructs_from_nsec(self) -> None:
        nsec = Keys.generate().secret_key().to_bech32()
        pub = Publisher.from_nsec(nsec, ["wss://relay/"])
        assert isinstance(pub.keys, Keys)
        assert len(pub.pubkey_hex) == 64

    def test_exposes_client_and_keys(self) -> None:
        nsec = Keys.generate().secret_key().to_bech32()
        pub = Publisher.from_nsec(nsec, ["wss://relay/"])
        assert pub.client is pub._client
        assert pub.keys is pub._keys

    async def test_context_manager_connects_and_disconnects(self) -> None:
        nsec = Keys.generate().secret_key().to_bech32()
        pub = Publisher.from_nsec(nsec, ["wss://relay/"])
        pub._client.add_relay = AsyncMock(return_value=True)
        pub._client.connect = AsyncMock()
        pub._client.disconnect = AsyncMock()
        async with pub:
            assert pub._connected is True
        assert pub._connected is False
        pub._client.connect.assert_awaited_once()
        pub._client.disconnect.assert_awaited_once()

    async def test_connect_is_idempotent(self) -> None:
        nsec = Keys.generate().secret_key().to_bech32()
        pub = Publisher.from_nsec(nsec, ["wss://relay/"])
        pub._client.add_relay = AsyncMock(return_value=True)
        pub._client.connect = AsyncMock()
        await pub.connect()
        await pub.connect()  # second call no-ops
        pub._client.connect.assert_awaited_once()

    async def test_disconnect_without_connect_is_safe(self) -> None:
        nsec = Keys.generate().secret_key().to_bech32()
        pub = Publisher.from_nsec(nsec, ["wss://relay/"])
        pub._client.disconnect = AsyncMock()
        await pub.disconnect()
        pub._client.disconnect.assert_not_awaited()

    async def test_post_note_delegates_to_send_note(self) -> None:
        nsec = Keys.generate().secret_key().to_bech32()
        pub = Publisher.from_nsec(nsec, ["wss://relay/"])
        pub._client.send_event_builder = AsyncMock(
            return_value=_output_mock(success=["wss://relay/"]),
        )
        result = await pub.post_note("hi", hashtags=["nostr"])
        assert result.kind == 1
        assert result.ok is True

    async def test_post_article_delegates_to_send_article(self) -> None:
        nsec = Keys.generate().secret_key().to_bech32()
        pub = Publisher.from_nsec(nsec, ["wss://relay/"])
        pub._client.send_event_builder = AsyncMock(
            return_value=_output_mock(success=["wss://relay/"]),
        )
        result = await pub.post_article("Title", "Body", identifier="a1")
        assert result.kind == 30023
        assert result.ok is True
