"""NostrBot: high-level bot runtime.

Wraps client lifecycle, profile publishing (kind 0 / 10002 / 10050),
subscriptions tuned for NIP-17's randomized timestamps, event dispatch
with dedup and per-user locks, protocol-matched DM reply, NIP-57 zap
validation, system push routing, periodic heartbeats, and graceful
shutdown.

Usage:

    cfg = NostrBotConfig(
        nsec="nsec1...",
        relays=["wss://relay.damus.io"],
        profile={"name": "Notible", "lud16": "notible@unsaltedbutter.ai"},
        zap_provider_pubkey="<hex>",
        accept_pushes_from={"<vps_bot_hex>": "vps"},
    )
    bot = NostrBot(cfg)

    @bot.on_dm
    async def dm(ctx, text): await ctx.reply("got it")

    @bot.on_zap
    async def zap(z, ctx): await ctx.reply(f"thanks for {z.amount_sats}")

    @bot.on_push("vps")
    async def push(content): ...

    @bot.on_heartbeat(interval=300)
    async def hb(uptime_s): ...

    await bot.run()
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nostr_sdk import (
    Client,
    Event,
    EventBuilder,
    Filter,
    HandleNotification,
    Keys,
    Kind,
    KindStandard,
    Metadata,
    NostrSigner,
    PublicKey,
    RelayMessage,
    RelayUrl,
    Tag,
    Timestamp,
    UnwrappedGift,
    nip04_decrypt,
)

from nostrbot_sdk.context import SenderContext
from nostrbot_sdk.dedup import Dedup
from nostrbot_sdk.expiration import expiration_tag
from nostrbot_sdk.identity import IdentityResolver
from nostrbot_sdk.locks import UserLockManager
from nostrbot_sdk.nip17_support import Nip17Support
from nostrbot_sdk.publishing import PublishResult, send_article, send_note
from nostrbot_sdk.zap_verify import ValidatedZap, validate_zap_receipt

log = logging.getLogger(__name__)


DmHandler = Callable[[SenderContext, str], Awaitable[None]]
ZapHandler = Callable[[ValidatedZap, SenderContext], Awaitable[None]]
PushHandler = Callable[[str], Awaitable[None]]
HeartbeatHandler = Callable[[int], Awaitable[None]]


@dataclass
class NostrBotConfig:
    """Configuration for NostrBot. Required vs optional documented per-field."""

    # -- Required --
    nsec: str
    """Bot's secret key in bech32 form (`nsec1...`) or 64-char hex."""

    relays: list[str]
    """Relays to connect to. First N (see inbox_relay_count) are advertised
    in the kind 10050 DM inbox list."""

    # -- Profile --
    profile: dict[str, Any] = field(default_factory=dict)
    """Kind 0 metadata as a dict. Keys: name, about, picture, lud16, nip05,
    website, banner, etc. If empty, kind 0 publish is skipped."""

    # -- Zap support --
    zap_provider_pubkey: str | None = None
    """Hex pubkey of your LNURL provider's signer. Required if you register
    an on_zap handler; without it NostrBot has no way to validate receipts."""

    # -- Push routing --
    accept_pushes_from: dict[str, str] = field(default_factory=dict)
    """Map of sender_hex -> route tag. DMs from these senders are routed to
    the matching `@bot.on_push("tag")` handler instead of the normal DM
    handler. The DM is NOT decrypted or transformed; the raw plaintext is
    passed to the push handler."""

    # -- Publishing knobs --
    inbox_relay_count: int = 2
    """How many of `relays` to advertise in kind 10050 as your DM inbox."""

    publish_kind_0: bool = True
    publish_kind_10002: bool = True
    publish_kind_10050: bool = True

    # -- Behavior --
    dm_expiry_seconds: int = 7 * 24 * 3600
    """NIP-40 expiration on outbound DMs. Default 7 days."""

    dedup_event_ttl_seconds: float = 600.0
    """How long an event_id is remembered to suppress relay redelivery."""

    dedup_content_ttl_seconds: float = 30.0
    """How long a (sender, content) pair is remembered to suppress the same
    DM arriving via both NIP-04 and NIP-17. Short, so genuine retries get
    through."""

    zap_dedup_ttl_seconds: float = 24 * 3600.0
    """How long a validated zap receipt's event id is remembered so a
    replayed kind 9735 (e.g., from a malicious or buggy relay) can't
    re-trigger on_zap. This is in-memory only: handlers that credit
    balances should additionally persist processed event ids so replays
    across bot restarts are also rejected."""

    user_protocol_ttl_seconds: float = 24 * 3600.0
    """How long the per-sender "last protocol seen" hint is kept. After it
    expires, send_dm falls back to the kind 10050 lookup. Also bounds the
    memory used to track senders in a long-running bot."""

    shutdown_grace_seconds: float = 10.0
    """How long stop() waits for in-flight DM/zap handler tasks to finish
    before cancelling them."""

    user_lock_idle_seconds: float = 300.0
    """How long a per-user lock can sit idle before cleanup."""

    nip17_cache_ttl_seconds: float = 3600.0
    """TTL for the kind 10050 (NIP-17 inbox) cache."""

    identity_cache_ttl_seconds: float = 3600.0
    """TTL for the kind 0 (Metadata) cache."""

    lock_cleanup_interval_seconds: float = 300.0
    """How often the background loop reaps idle locks and expired caches."""


class NostrBot:
    """High-level Nostr bot runtime.

    Construct, register handlers with `@bot.on_dm`/`@bot.on_zap`/
    `@bot.on_push(tag)`/`@bot.on_heartbeat(interval)`, then call
    `await bot.run()` (or `start()` + your own loop driver).
    """

    def __init__(self, config: NostrBotConfig) -> None:
        self._config = config
        self._keys = Keys.parse(config.nsec)
        self._signer = NostrSigner.keys(self._keys)
        self._client = Client(self._signer)
        self._pubkey_hex = self._keys.public_key().to_hex()

        self._dedup_event: Dedup[str] = Dedup(
            ttl_seconds=config.dedup_event_ttl_seconds,
        )
        self._dedup_content: Dedup[tuple[str, str]] = Dedup(
            ttl_seconds=config.dedup_content_ttl_seconds,
        )
        self._dedup_zap: Dedup[str] = Dedup(
            ttl_seconds=config.zap_dedup_ttl_seconds,
        )
        self._locks = UserLockManager(idle_seconds=config.user_lock_idle_seconds)
        self._nip17_support = Nip17Support(
            self._client, ttl_seconds=config.nip17_cache_ttl_seconds,
        )
        self._identity = IdentityResolver(
            self._client, ttl_seconds=config.identity_cache_ttl_seconds,
        )

        # Per-user "last protocol seen" used by send_dm to match replies.
        # Values are (protocol, monotonic_timestamp); entries older than
        # user_protocol_ttl_seconds are ignored and pruned by maintenance.
        self._user_protocol: dict[str, tuple[str, float]] = {}

        # start_time gate to drop events older than the bot's startup time.
        # Set in start(), so events before start() are not dropped.
        self._start_time: Timestamp | None = None

        # Handler registry.
        self._dm_handler: DmHandler | None = None
        self._zap_handler: ZapHandler | None = None
        self._push_handlers: dict[str, PushHandler] = {}
        self._heartbeat_handlers: list[tuple[int, HeartbeatHandler]] = []

        # Lifecycle.
        self._tasks: list[asyncio.Task] = []
        # Per-event handler tasks spawned by _NotifyBridge. Strong references
        # are required: asyncio keeps only weak refs to tasks, so an untracked
        # fire-and-forget task can be garbage-collected mid-flight. stop()
        # also waits for these so in-flight DMs aren't dropped on shutdown.
        self._inflight: set[asyncio.Task] = set()
        self._shutdown = asyncio.Event()
        self._started = False

    # -- Properties for advanced/escape-hatch use ------------------------------

    @property
    def client(self) -> Client:
        return self._client

    @property
    def keys(self) -> Keys:
        return self._keys

    @property
    def signer(self) -> NostrSigner:
        return self._signer

    @property
    def pubkey_hex(self) -> str:
        return self._pubkey_hex

    @property
    def identity(self) -> IdentityResolver:
        return self._identity

    @property
    def nip17_support(self) -> Nip17Support:
        return self._nip17_support

    # -- Handler registration (decorators) -------------------------------------

    def on_dm(self, fn: DmHandler) -> DmHandler:
        """Register the DM handler. Last call wins."""
        self._dm_handler = fn
        return fn

    def on_zap(self, fn: ZapHandler) -> ZapHandler:
        """Register the zap handler. Last call wins.

        Requires `zap_provider_pubkey` in the config; raises ValueError
        otherwise so misconfiguration fails loudly at startup.
        """
        if not self._config.zap_provider_pubkey:
            raise ValueError(
                "on_zap requires NostrBotConfig.zap_provider_pubkey to be set",
            )
        self._zap_handler = fn
        return fn

    def on_push(self, tag: str) -> Callable[[PushHandler], PushHandler]:
        """Register a push handler for a tag declared in accept_pushes_from."""
        declared_tags = set(self._config.accept_pushes_from.values())
        if tag not in declared_tags:
            raise ValueError(
                f"push tag {tag!r} is not declared in "
                f"NostrBotConfig.accept_pushes_from (known: {sorted(declared_tags)})",
            )

        def decorator(fn: PushHandler) -> PushHandler:
            self._push_handlers[tag] = fn
            return fn

        return decorator

    def on_heartbeat(
        self, interval: int = 300,
    ) -> Callable[[HeartbeatHandler], HeartbeatHandler]:
        """Register a heartbeat callback fired every `interval` seconds.

        The callback receives the bot's uptime in seconds (monotonic clock).
        Multiple heartbeats with different intervals can be registered.
        """
        if interval <= 0:
            raise ValueError("heartbeat interval must be positive")

        def decorator(fn: HeartbeatHandler) -> HeartbeatHandler:
            self._heartbeat_handlers.append((interval, fn))
            return fn

        return decorator

    # -- DM sending ------------------------------------------------------------

    async def send_dm(self, recipient_hex: str, text: str) -> bool:
        """Send a DM to `recipient_hex` using their preferred protocol.

        Priority:
          1. If `recipient_hex` recently sent us a DM via NIP-04, reply
             via NIP-04.
          2. Otherwise consult their kind 10050. If found, NIP-17 to those
             inbox relays (added permanently to the pool to avoid TIME_WAIT
             socket churn). This applies to NIP-17 repliers too: without
             gossip, `send_private_msg` would publish only to OUR relay
             pool, which the recipient may never read.
          3. If they reached us via NIP-17 but no 10050 is found right now,
             best-effort NIP-17 to our own pool.
          4. Fall back to NIP-04.

        Returns True if the send succeeded, False if it failed (errors are
        logged, never raised).
        """
        try:
            pk = PublicKey.parse(recipient_hex)
            protocol = self._get_user_protocol(recipient_hex)
            exp = expiration_tag(self._config.dm_expiry_seconds)

            if protocol == "nip04":
                await self._send_nip04(pk, text, exp)
                log.info("Sent nip04 DM to %s (%d chars)", recipient_hex[:16], len(text))
                return True

            dm_relays = await self._nip17_support.check(recipient_hex)
            if dm_relays:
                relay_urls = [RelayUrl.parse(r) for r in dm_relays]
                added: list[RelayUrl] = []
                for ru in relay_urls:
                    if await self._client.add_relay(ru):
                        added.append(ru)
                if added:
                    await self._client.connect()
                await self._client.send_private_msg_to(
                    relay_urls, pk, text, [exp],
                )
                log.info(
                    "Sent nip17 DM to %s (%d chars, %d inbox relays)",
                    recipient_hex[:16], len(text), len(dm_relays),
                )
            elif protocol == "nip17":
                # They DM'd us via NIP-17 so they do support it, but we can't
                # find their inbox relays right now. Best effort: gift wrap
                # to our own pool and hope for relay overlap.
                await self._client.send_private_msg(pk, text, [exp])
                log.info(
                    "Sent nip17 DM to %s (%d chars, own pool; no kind 10050 found)",
                    recipient_hex[:16], len(text),
                )
            else:
                await self._send_nip04(pk, text, exp)
                log.info(
                    "Sent nip04 DM to %s (%d chars, fallback)",
                    recipient_hex[:16], len(text),
                )
            return True
        except Exception:
            log.exception("Failed to send DM to %s", recipient_hex[:16])
            return False

    def _set_user_protocol(self, sender_hex: str, protocol: str) -> None:
        self._user_protocol[sender_hex] = (protocol, time.monotonic())

    def _get_user_protocol(self, sender_hex: str) -> str | None:
        entry = self._user_protocol.get(sender_hex)
        if entry is None:
            return None
        protocol, ts = entry
        if time.monotonic() - ts > self._config.user_protocol_ttl_seconds:
            self._user_protocol.pop(sender_hex, None)
            return None
        return protocol

    def _prune_user_protocol(self) -> int:
        """Drop expired protocol hints. Returns count removed."""
        now = time.monotonic()
        ttl = self._config.user_protocol_ttl_seconds
        stale = [
            k for k, (_, ts) in self._user_protocol.items() if now - ts > ttl
        ]
        for k in stale:
            del self._user_protocol[k]
        return len(stale)

    async def _send_nip04(self, pk: PublicKey, text: str, exp_tag: Tag) -> None:
        ciphertext = await self._signer.nip04_encrypt(pk, text)
        builder = EventBuilder(Kind(4), ciphertext).tags([
            Tag.parse(["p", pk.to_hex()]),
            exp_tag,
        ])
        await self._client.send_event_builder(builder)

    # -- Publishing (kind 1 notes and kind 30023 long-form articles) ---------

    async def post_note(self, content: str, **kw) -> PublishResult:
        """Publish a kind 1 note. See nostrbot_sdk.publishing.send_note for kwargs.

        Reuses the bot's connected client (no per-call connect/disconnect).
        Common kwargs: reply_to, reply_root, reply_to_author, quote,
        quote_author, mention_pubkeys, hashtags, expiration_seconds,
        extra_tags.
        """
        return await send_note(self._client, content, **kw)

    async def post_article(self, title: str, content: str, **kw) -> PublishResult:
        """Publish a NIP-23 long-form article (kind 30023).

        Required kwarg: identifier (the `d`-tag value).
        Optional: summary, image, hashtags, published_at, extra_tags.
        """
        return await send_article(self._client, title, content, **kw)

    # -- Lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Connect to relays, publish profile/relay lists, subscribe.

        After start() returns, the bot is processing events. Call stop() to
        shut down or use run() which manages the full lifecycle including
        signal handling.
        """
        if self._started:
            raise RuntimeError("NostrBot already started")
        # Allow start() after a prior stop(): background loops and run()
        # watch this event, so a stale set() would make them exit instantly.
        self._shutdown = asyncio.Event()

        # 1. Add relays + connect.
        for relay in self._config.relays:
            await self._client.add_relay(RelayUrl.parse(relay))
        await self._client.connect()
        log.info("Connected to %d relay(s)", len(self._config.relays))

        # 2. Publish kind 0 (Metadata) if profile is non-empty.
        if self._config.publish_kind_0 and self._config.profile:
            metadata = Metadata.from_json(json.dumps(self._config.profile))
            await self._client.set_metadata(metadata)
            log.info("Published kind 0 profile")

        # 3. Publish kind 10002 (NIP-65 relay list).
        if self._config.publish_kind_10002:
            relay_map = {RelayUrl.parse(r): None for r in self._config.relays}
            await self._client.send_event_builder(
                EventBuilder.relay_list(relay_map),
            )
            log.info(
                "Published kind 10002 relay list (%d relays)", len(relay_map),
            )

        # 4. Publish kind 10050 (NIP-17 DM inbox list).
        if self._config.publish_kind_10050:
            inbox = self._config.relays[: self._config.inbox_relay_count]
            inbox_tags = [Tag.parse(["relay", r]) for r in inbox]
            await self._client.send_event_builder(
                EventBuilder(Kind(10050), "").tags(inbox_tags),
            )
            log.info("Published kind 10050 DM inbox list (%d relays)", len(inbox))

        # 5. Subscribe.
        #    Kind 4 + 9735: .since(now) is safe.
        #    Kind 1059: NIP-17 randomizes created_at up to 2 days back, so
        #    .since(now) drops them. Use .limit(0) to receive new ones only.
        self._start_time = Timestamp.now()
        bot_pk = self._keys.public_key()

        f_legacy = (
            Filter()
            .pubkey(bot_pk)
            .kinds([Kind(4), Kind.from_std(KindStandard.ZAP_RECEIPT)])
            .since(self._start_time)
        )
        f_giftwrap = (
            Filter()
            .pubkey(bot_pk)
            .kind(Kind.from_std(KindStandard.GIFT_WRAP))
            .limit(0)
        )
        # Start consuming notifications BEFORE subscribing so events that
        # arrive immediately after the subscription are not dropped.
        self._tasks.append(asyncio.create_task(
            self._client.handle_notifications(_NotifyBridge(self)),
            name="nostrbot_notifications",
        ))
        await self._client.subscribe(f_legacy)
        await self._client.subscribe(f_giftwrap)
        log.info("Subscribed to kind 4, 1059, 9735")

        # 6. Background tasks.
        start_mono = time.monotonic()
        self._tasks.append(asyncio.create_task(
            self._maintenance_loop(),
            name="nostrbot_maintenance",
        ))
        for interval, handler in self._heartbeat_handlers:
            self._tasks.append(asyncio.create_task(
                self._heartbeat_loop(interval, handler, start_mono),
                name=f"nostrbot_heartbeat_{interval}s",
            ))

        self._started = True
        log.info(
            "NostrBot running (pubkey: %s)", self._keys.public_key().to_bech32(),
        )

    async def stop(self) -> None:
        """Cancel background tasks, drain in-flight handlers, disconnect.

        In-flight DM/zap handler tasks get `shutdown_grace_seconds` to
        finish (the client is still connected, so they can reply); whatever
        is left after the grace period is cancelled.
        """
        if not self._started:
            return
        self._shutdown.set()
        # Cancel background loops first: this stops the notification bridge,
        # so no NEW handler tasks are spawned while we drain.
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Task error on shutdown")
        self._tasks.clear()

        pending = [t for t in self._inflight if not t.done()]
        if pending:
            log.info(
                "Waiting up to %.0fs for %d in-flight handler(s)",
                self._config.shutdown_grace_seconds, len(pending),
            )
            _, still_pending = await asyncio.wait(
                pending, timeout=self._config.shutdown_grace_seconds,
            )
            if still_pending:
                log.warning(
                    "Cancelling %d handler(s) still running after grace period",
                    len(still_pending),
                )
                for t in still_pending:
                    t.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)
        self._inflight.clear()

        try:
            await self._client.disconnect()
        except Exception:
            log.exception("Error disconnecting client")
        self._started = False
        log.info("NostrBot stopped")

    async def run(self) -> None:
        """Start, install signal handlers for SIGINT/SIGTERM, run until signal.

        Convenience wrapper. If you need to interleave with other async
        services, use start() + your own shutdown trigger + stop() instead.
        """
        await self.start()
        loop = asyncio.get_running_loop()
        handlers_installed: list[int] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
                handlers_installed.append(sig)
            except (NotImplementedError, RuntimeError):
                # Windows or non-main thread: handler skipped.
                pass
        try:
            await self._shutdown.wait()
            log.info("Shutdown signal received")
        finally:
            for sig in handlers_installed:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            await self.stop()

    # -- Event dispatch --------------------------------------------------------

    async def _handle_event(self, event: Event) -> None:
        """Route an inbound event. Called by _NotifyBridge per nostr-sdk event."""
        eid = event.id().to_hex()
        if self._dedup_event.check_and_add(eid):
            log.debug("Skipping duplicate event %s", eid[:16])
            return

        kind = event.kind()
        try:
            if kind == Kind(4):
                await self._handle_nip04_dm(event)
            elif kind.as_std() == KindStandard.GIFT_WRAP:
                await self._handle_nip17_dm(event)
            elif kind.as_std() == KindStandard.ZAP_RECEIPT:
                await self._handle_zap_receipt(event)
        except Exception:
            log.exception(
                "Error handling event %s (kind %s)", eid[:16], kind.as_u16(),
            )

    async def _handle_nip04_dm(self, event: Event) -> None:
        if self._start_time and event.created_at().as_secs() < self._start_time.as_secs():
            return
        sender_pk = event.author()
        sender_hex = sender_pk.to_hex()
        try:
            plaintext = nip04_decrypt(
                self._keys.secret_key(), sender_pk, event.content(),
            )
        except Exception:
            log.warning(
                "Failed to decrypt NIP-04 DM from %s", sender_hex[:16],
            )
            return
        await self._route_inbound(sender_pk, sender_hex, plaintext, "nip04")

    async def _handle_nip17_dm(self, event: Event) -> None:
        try:
            unwrapped = await UnwrappedGift.from_gift_wrap(self._signer, event)
        except Exception:
            log.warning("Failed to unwrap NIP-17 gift wrap")
            return

        sender_pk: PublicKey = unwrapped.sender()
        rumor = unwrapped.rumor()

        if self._start_time and rumor.created_at().as_secs() < self._start_time.as_secs():
            return
        if rumor.kind().as_std() != KindStandard.PRIVATE_DIRECT_MESSAGE:
            return

        sender_hex = sender_pk.to_hex()
        await self._route_inbound(sender_pk, sender_hex, rumor.content(), "nip17")

    async def _route_inbound(
        self,
        sender_pk: PublicKey,
        sender_hex: str,
        content: str,
        protocol: str,
    ) -> None:
        """Route an inbound DM to a push handler or the user DM handler."""
        content_key = (sender_hex, content.strip())

        # System push (sender is a known backend)?
        push_tag = self._config.accept_pushes_from.get(sender_hex)
        if push_tag is not None:
            handler = self._push_handlers.get(push_tag)
            if handler is None:
                log.warning(
                    "Push DM from %s (tag %r) but no handler registered",
                    sender_hex[:16], push_tag,
                )
                return
            # Backends sending via both NIP-04 and NIP-17 for compat need
            # the same content dedup as user DMs.
            if self._dedup_content.check_and_add(content_key):
                log.debug(
                    "Duplicate push content from %s, skipping", sender_hex[:16],
                )
                return
            try:
                await handler(content)
            except Exception:
                self._dedup_content.forget(content_key)
                log.exception(
                    "Push handler %r failed for sender %s",
                    push_tag, sender_hex[:16],
                )
            return

        # User DM.
        self._set_user_protocol(sender_hex, protocol)

        if self._dedup_content.check_and_add(content_key):
            log.debug("Duplicate content from %s, skipping", sender_hex[:16])
            return

        if self._dm_handler is None:
            log.debug(
                "No on_dm handler registered; dropping DM from %s",
                sender_hex[:16],
            )
            return

        ctx = SenderContext(
            sender_hex=sender_hex,
            sender_npub=sender_pk.to_bech32(),
            protocol=protocol,  # type: ignore[arg-type]
            _bot=self,
        )
        lock = self._locks.get(sender_hex)
        try:
            async with lock:
                await self._dm_handler(ctx, content)
        except Exception:
            # Roll back the content dedup so the user can retry.
            self._dedup_content.forget(content_key)
            log.exception(
                "DM handler failed for %s; content dedup cleared so retry works",
                sender_hex[:16],
            )

    async def _handle_zap_receipt(self, event: Event) -> None:
        if self._start_time and event.created_at().as_secs() < self._start_time.as_secs():
            return
        if self._zap_handler is None or not self._config.zap_provider_pubkey:
            return

        zap = validate_zap_receipt(
            event,
            bot_pubkey_hex=self._pubkey_hex,
            zap_provider_pubkey_hex=self._config.zap_provider_pubkey,
        )
        if zap is None:
            return

        # Long-TTL replay guard: the general event dedup (10 min) is too
        # short for payment-grade idempotency; a relay redelivering a valid
        # receipt later must not credit the sender twice.
        if self._dedup_zap.check_and_add(zap.event_id):
            log.warning(
                "Replayed zap receipt %s from %s ignored",
                zap.event_id[:16], zap.sender_hex[:16],
            )
            return

        try:
            sender_pk = PublicKey.parse(zap.sender_hex)
        except Exception:
            log.warning("Validated zap has unparseable sender %s", zap.sender_hex)
            return

        ctx = SenderContext(
            sender_hex=zap.sender_hex,
            sender_npub=sender_pk.to_bech32(),
            protocol="zap",
            _bot=self,
        )
        try:
            await self._zap_handler(zap, ctx)
        except Exception:
            log.exception("Zap handler failed for %s", zap.sender_hex[:16])

    # -- Background loops ------------------------------------------------------

    async def _heartbeat_loop(
        self,
        interval: int,
        handler: HeartbeatHandler,
        start_mono: float,
    ) -> None:
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=interval,
                    )
                    return  # shutdown received
                except asyncio.TimeoutError:
                    pass
                try:
                    uptime = int(time.monotonic() - start_mono)
                    await handler(uptime)
                except Exception:
                    log.exception("Heartbeat handler error")
        except asyncio.CancelledError:
            raise

    async def _maintenance_loop(self) -> None:
        """Periodic cleanup: idle locks, expired caches."""
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self._config.lock_cleanup_interval_seconds,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    self._locks.cleanup_idle()
                    self._nip17_support.cleanup_expired()
                    self._identity.cleanup_expired()
                    self._prune_user_protocol()
                except Exception:
                    log.exception("Maintenance loop error")
        except asyncio.CancelledError:
            raise


class _NotifyBridge(HandleNotification):
    """Bridge between nostr-sdk's notification system and NostrBot._handle_event.

    Spawns a task per event so unrelated events process in parallel; same-user
    events serialize via NostrBot's per-user lock. Tasks are tracked in the
    bot's _inflight set: asyncio holds only weak references to tasks, so an
    untracked task could be garbage-collected mid-flight, and stop() needs
    the set to drain handlers gracefully.
    """

    def __init__(self, bot: NostrBot) -> None:
        self._bot = bot

    async def handle(
        self, relay_url: RelayUrl, subscription_id: str, event: Event,
    ) -> None:
        task = asyncio.create_task(self._bot._handle_event(event))
        self._bot._inflight.add(task)
        task.add_done_callback(self._bot._inflight.discard)

    async def handle_msg(self, relay_url: RelayUrl, msg: RelayMessage) -> None:
        pass
