"""Publishing notes and long-form articles with sane NIP-10 / NIP-18 / NIP-23 defaults.

Two ways in:

  1. `NostrBot.post_note(...)` / `NostrBot.post_article(...)` on an already-running
     bot, reusing its connected client.

  2. `Publisher` for one-shot scripts (cron, CLI tools) that just publish and exit:

         async with Publisher.from_nsec(nsec, relays) as pub:
             result = await pub.post_note("hello", hashtags=["nostr"])
             print(result.note_id, result.success_relays)

Both paths return a `PublishResult` so callers can detect partial / total publish
failure instead of guessing from log output.

Footguns this module hides:

  * NIP-10 reply threading: marked `e`-tags ("root" / "reply"), legacy positional
    rules. Caller passes `reply_to=<parent_id>` (and optionally `reply_root=`);
    the right tags get emitted.
  * NIP-18 quoting: `q`-tag plus a `p`-tag for the quoted author. Caller passes
    `quote=<event_id>` (and optionally `quote_author=`).
  * Hashtag normalization (NIP-12): strip leading `#`, lowercase.
  * Long-form (kind 30023) needs `d` / `title` / `summary` / `image` /
    `published_at` tags. `post_article` builds them.
  * Client TIME_WAIT churn: `Publisher` opens one Client for a script's
    lifetime; `NostrBot.post_note` reuses the bot's existing pool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nostr_sdk import (
    AsyncNostrSigner,
    Client,
    EventBuilder,
    Keys,
    Kind,
    PublicKey,
    RelayUrl,
    Tag,
)

from nostrbot_sdk.expiration import expiration_tag

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# -- Result ----------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    """Result of publishing an event.

    Attributes:
        event_id: 64-char hex event id.
        note_id: bech32 encoding of the event id (`note1...`). For kind 30023
            this is still the event-id bech32; the canonical "addressable"
            reference is an `naddr1` which callers can build from
            (kind, author_pubkey, identifier) themselves.
        success_relays: relay URLs that accepted the event.
        failed_relays: relay URL -> error message for relays that rejected.
        kind: the event kind (1 for note, 30023 for long-form).
    """

    event_id: str
    note_id: str
    success_relays: list[str]
    failed_relays: dict[str, str]
    kind: int

    @property
    def ok(self) -> bool:
        """True iff at least one relay accepted the event."""
        return len(self.success_relays) > 0

    @property
    def relay_count(self) -> int:
        return len(self.success_relays)


# -- Helpers ---------------------------------------------------------------


def normalize_hashtag(tag: str) -> str:
    """NIP-12 convention: strip whitespace, drop a leading `#`, lowercase. Empty -> ''."""
    return tag.strip().lstrip("#").strip().lower()


def build_note_tags(
    *,
    reply_to: str | None = None,
    reply_root: str | None = None,
    reply_to_author: str | None = None,
    quote: str | None = None,
    quote_author: str | None = None,
    mention_pubkeys: list[str] | None = None,
    hashtags: list[str] | None = None,
    expiration_seconds: int | None = None,
    extra_tags: list[Tag] | None = None,
) -> list[Tag]:
    """Build the tag list for a kind 1 note.

    NIP-10 reply threading:
      - `reply_to` is the immediate parent event id (hex).
      - `reply_root` is the thread root. Omit if `reply_to` IS the root
        (direct reply to top-level post).
      - `reply_to_author` is the parent author's pubkey (hex). Highly
        recommended: NIP-10 expects replies to `p`-tag the people being
        replied to; without it the parent author gets no notification.
      - Emits marked `e`-tags: "root" for the root, "reply" for the parent
        (only when they differ).

    NIP-18 quoting:
      - `quote` is the event id (hex) being quoted.
      - `quote_author` is the quoted author's pubkey (hex). Highly recommended:
        adds a `p`-tag so the quoted author sees it in their notifications.

    Other:
      - `mention_pubkeys`: additional `p`-tags (deduped against
        reply_to_author / quote_author).
      - `hashtags`: emitted as lowercased `t`-tags.
      - `expiration_seconds`: NIP-40 expiration in seconds from now.
      - `extra_tags`: any custom tags appended verbatim at the end.
    """
    tags: list[Tag] = []
    p_tags_seen: set[str] = set()

    # NIP-10 reply threading.
    if reply_to:
        root = reply_root or reply_to
        tags.append(Tag.parse(["e", root, "", "root"]))
        if reply_root and reply_root != reply_to:
            tags.append(Tag.parse(["e", reply_to, "", "reply"]))
        if reply_to_author:
            tags.append(Tag.public_key(PublicKey.parse(reply_to_author)))
            p_tags_seen.add(reply_to_author)

    # NIP-18 quote.
    if quote:
        if quote_author:
            tags.append(Tag.parse(["q", quote, "", quote_author]))
            if quote_author not in p_tags_seen:
                tags.append(Tag.public_key(PublicKey.parse(quote_author)))
                p_tags_seen.add(quote_author)
        else:
            tags.append(Tag.parse(["q", quote]))

    # Mention p-tags (deduped).
    if mention_pubkeys:
        for pk_hex in mention_pubkeys:
            if pk_hex and pk_hex not in p_tags_seen:
                tags.append(Tag.public_key(PublicKey.parse(pk_hex)))
                p_tags_seen.add(pk_hex)

    # Hashtag t-tags (normalized).
    if hashtags:
        seen_t: set[str] = set()
        for raw in hashtags:
            cleaned = normalize_hashtag(raw)
            if cleaned and cleaned not in seen_t:
                tags.append(Tag.parse(["t", cleaned]))
                seen_t.add(cleaned)

    # NIP-40 expiration.
    if expiration_seconds is not None:
        tags.append(expiration_tag(expiration_seconds))

    # Caller-supplied extras (appended verbatim).
    if extra_tags:
        tags.extend(extra_tags)

    return tags


def build_article_tags(
    *,
    identifier: str,
    title: str,
    summary: str | None = None,
    image: str | None = None,
    hashtags: list[str] | None = None,
    published_at: int | None = None,
    extra_tags: list[Tag] | None = None,
) -> list[Tag]:
    """Build the tag list for a NIP-23 long-form article (kind 30023).

    `identifier` is the `d`-tag value, unique per (author, kind). Editing
    an article means publishing kind 30023 again with the same `d`-tag;
    relays replace the prior version.
    """
    if not identifier:
        raise ValueError("post_article: identifier (d-tag) is required")

    tags: list[Tag] = [
        Tag.parse(["d", identifier]),
        Tag.parse(["title", title]),
    ]
    if summary:
        tags.append(Tag.parse(["summary", summary]))
    if image:
        tags.append(Tag.parse(["image", image]))
    if published_at is not None:
        tags.append(Tag.parse(["published_at", str(published_at)]))
    if hashtags:
        seen_t: set[str] = set()
        for raw in hashtags:
            cleaned = normalize_hashtag(raw)
            if cleaned and cleaned not in seen_t:
                tags.append(Tag.parse(["t", cleaned]))
                seen_t.add(cleaned)
    if extra_tags:
        tags.extend(extra_tags)
    return tags


# -- Send (shared by NostrBot and Publisher) -------------------------------


async def send_note(
    client: Client,
    content: str,
    *,
    signer: AsyncNostrSigner,
    reply_to: str | None = None,
    reply_root: str | None = None,
    reply_to_author: str | None = None,
    quote: str | None = None,
    quote_author: str | None = None,
    mention_pubkeys: list[str] | None = None,
    hashtags: list[str] | None = None,
    expiration_seconds: int | None = None,
    extra_tags: list[Tag] | None = None,
) -> PublishResult:
    """Publish a kind 1 note using `client`, signed with `signer`.

    `signer` is required because nostr-sdk >=0.45 clients no longer hold one.
    Returns PublishResult.
    """
    tags = build_note_tags(
        reply_to=reply_to,
        reply_root=reply_root,
        reply_to_author=reply_to_author,
        quote=quote,
        quote_author=quote_author,
        mention_pubkeys=mention_pubkeys,
        hashtags=hashtags,
        expiration_seconds=expiration_seconds,
        extra_tags=extra_tags,
    )
    builder = EventBuilder(Kind(1), content).tags(tags)
    return await _send(client, signer, builder, kind=1)


async def send_article(
    client: Client,
    title: str,
    content: str,
    *,
    signer: AsyncNostrSigner,
    identifier: str,
    summary: str | None = None,
    image: str | None = None,
    hashtags: list[str] | None = None,
    published_at: int | None = None,
    extra_tags: list[Tag] | None = None,
) -> PublishResult:
    """Publish a NIP-23 long-form article (kind 30023), signed with `signer`.

    `signer` is required because nostr-sdk >=0.45 clients no longer hold one.
    Returns PublishResult.
    """
    tags = build_article_tags(
        identifier=identifier,
        title=title,
        summary=summary,
        image=image,
        hashtags=hashtags,
        published_at=published_at,
        extra_tags=extra_tags,
    )
    builder = EventBuilder(Kind(30023), content).tags(tags)
    return await _send(client, signer, builder, kind=30023)


async def _send(
    client: Client,
    signer: AsyncNostrSigner,
    builder: EventBuilder,
    *,
    kind: int,
) -> PublishResult:
    # nostr-sdk >=0.45 removed send_event_builder: sign first, then send.
    event = await builder.finalize_async(signer)
    output = await client.send_event(event)
    event_id_hex = output.id.to_hex()
    note_id = output.id.to_bech32()
    success_relays = [str(r) for r in output.success]
    failed_relays = {str(r): str(err) for r, err in output.failed.items()}

    if not success_relays:
        log.warning(
            "Publish kind %d %s: zero relays accepted (failed: %s)",
            kind, event_id_hex[:16], list(failed_relays.keys()),
        )
    else:
        log.info(
            "Published kind %d %s to %d relay(s)",
            kind, event_id_hex[:16], len(success_relays),
        )

    return PublishResult(
        event_id=event_id_hex,
        note_id=note_id,
        success_relays=success_relays,
        failed_relays=failed_relays,
        kind=kind,
    )


# -- Publisher (one-shot script convenience) -------------------------------


class Publisher:
    """Minimal Nostr publisher for one-shot scripts.

    Holds one Client + Keys for the lifetime of the script. For long-running
    bots, prefer NostrBot.post_note / NostrBot.post_article which reuse the
    bot's already-connected client.

    Usage:

        async with Publisher.from_nsec(nsec, relays) as pub:
            result = await pub.post_note("hello", hashtags=["nostr"])
            print(result.ok, result.success_relays)
    """

    def __init__(self, keys: Keys, relays: list[str]) -> None:
        self._keys = keys
        # nostr-sdk >=0.45: Keys is itself a signer and Client takes none.
        self._signer = keys
        self._client = Client()
        self._relays = list(relays)
        self._connected = False

    @classmethod
    def from_nsec(cls, nsec: str, relays: list[str]) -> "Publisher":
        return cls(Keys.parse(nsec), relays)

    @property
    def client(self) -> Client:
        return self._client

    @property
    def keys(self) -> Keys:
        return self._keys

    @property
    def pubkey_hex(self) -> str:
        return self._keys.public_key().to_hex()

    async def connect(self) -> None:
        if self._connected:
            return
        for relay in self._relays:
            await self._client.add_relay(RelayUrl.parse(relay))
        await self._client.connect()
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            await self._client.disconnect()
        finally:
            self._connected = False

    async def __aenter__(self) -> "Publisher":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    async def post_note(self, content: str, **kw) -> PublishResult:
        return await send_note(self._client, content, signer=self._signer, **kw)

    async def post_article(self, title: str, content: str, **kw) -> PublishResult:
        return await send_article(
            self._client, title, content, signer=self._signer, **kw,
        )
