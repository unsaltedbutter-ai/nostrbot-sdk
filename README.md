# nostrbot-sdk

High-level toolkit for building Nostr bots in Python. Handles the protocol plumbing
(DM listening, NIP-17 gift wraps, zap receipt validation, LNURL-pay) so your bot code
can stay focused on what it actually does.

Built on [`nostr-sdk`](https://pypi.org/project/nostr-sdk/) (pinned to `0.44.2`).

**Status:** v0.1.0 ships the three pure primitives below. v0.2.0 (the high-level
`NostrBot` runtime) and v0.3.0 (`LnurlPayer`) are in active development; see
[Roadmap](#roadmap). Items marked **(v0.2.0)** or **(v0.3.0)** below describe the
target API and behavior, not what is callable from `pip install nostrbot-sdk` today.

## Why this exists

Every Nostr bot I write ends up with the same 400 lines of boilerplate: relay setup,
subscriptions tuned for kind 1059's randomized timestamps, NIP-04 + NIP-17 decryption,
per-user locks to serialize state, protocol-matched replies, kind 10050 caching,
NIP-57 zap validation, socket-leak avoidance. None of it is hard, but all of it is
easy to get subtly wrong and tedious to rebuild from scratch. This package factors it
out.

## Quickstart (target API, partially shipped)

```python
from nostrbot_sdk import NostrBot, NostrBotConfig, ValidatedZap, SenderContext

cfg = NostrBotConfig(
    nsec="nsec1...",
    relays=["wss://relay.damus.io", "wss://nos.lol"],
    profile={
        "name": "ButterBot",
        "about": "an example bot",
        "lud16": "butterbot@unsaltedbutter.ai",
    },
    zap_provider_pubkey="<hex of your LNURL provider's nostr pubkey>",
)

bot = NostrBot(cfg)

@bot.on_dm
async def handle_dm(ctx: SenderContext, text: str) -> None:
    await ctx.reply(f"echo: {text}")

@bot.on_zap
async def handle_zap(zap: ValidatedZap, ctx: SenderContext) -> None:
    await ctx.reply(f"thanks for the {zap.amount_sats:,} sats")

await bot.run()
```

---

## Features

Each section below describes the problem the feature solves and how to use it. Items
marked **(v0.1.0)** are shipped today. Items marked **(v0.2.0)** describe the target
API of the next release, currently in development. Items marked **(v0.3.0)** are
queued behind that.

### NIP-17 inbox detection with TTL cache (v0.1.0)

`Nip17Support` checks whether a recipient has published a kind 10050 event listing
their preferred DM inbox relays.

**Why it matters.** NIP-17 (gift-wrapped DMs) is the modern, metadata-private way to
send Nostr DMs, but it only works if the recipient publishes a kind 10050 telling you
which relays to deliver to. Naively sending NIP-17 to every relay you happen to be
connected to is wasteful and often fails. Querying for kind 10050 on every outbound
DM, on the other hand, is slow and rude to relays. The cache holds both positive and
negative results (default TTL 1 hour) so a typical conversation does one lookup.

**Usage.**

```python
from nostrbot_sdk import Nip17Support
from nostr_sdk import RelayUrl

detector = Nip17Support(client, ttl_seconds=3600)

dm_relays = await detector.check(recipient_pubkey_hex)
if dm_relays is not None:
    relay_urls = [RelayUrl.parse(r) for r in dm_relays]
    await client.send_private_msg_to(relay_urls, pk, "hello", [])
else:
    # No kind 10050: fall back to NIP-04 (legacy)
    ...

# When you know a user changed their relay list (e.g., from a 10050 event you received)
detector.invalidate(recipient_pubkey_hex)
```

### NIP-57 zap receipt validation (v0.1.0)

`validate_zap_receipt(event, bot_pubkey_hex, zap_provider_pubkey_hex) -> ValidatedZap | None`
runs all six NIP-57 protocol checks on a kind 9735 event and returns the parsed
sender, amount, and event id on success.

**Why it matters.** Zap receipts are unauthenticated by default: anyone can publish a
kind 9735 claiming "user X zapped you 1000 sats." If you trust receipts blindly you
will pay out goods or services for free. NIP-57 defines six checks that together
prove the zap is real, the right amount, and addressed to you. Getting any one of
them wrong is a free-money exploit. This function is pure (no I/O), takes a few
microseconds, and has 14 test cases covering each failure mode.

The six checks:

1. The 9735 author is your configured LNURL provider's nostr pubkey (not an attacker)
2. The bolt11 `description_hash` equals SHA-256 of the `description` tag
3. The embedded 9734 zap request has a valid signature
4. The 9734 is actually kind 9734
5. The 9734's `p` tag references your bot's pubkey (this zap was for you)
6. If the 9734 has an `amount` tag, it matches the bolt11 amount

**Usage.**

```python
from nostrbot_sdk import validate_zap_receipt, ValidatedZap

# Inside your kind 9735 event handler:
zap = validate_zap_receipt(
    event,
    bot_pubkey_hex=my_pubkey_hex,
    zap_provider_pubkey_hex=lnurl_provider_pubkey_hex,
)
if zap is None:
    return  # Invalid; logged with reason

# zap.sender_hex, zap.amount_sats, zap.event_id, zap.bolt11
await credit_user(zap.sender_hex, zap.amount_sats)
```

### NIP-40 expiration tag helper (v0.1.0)

`expiration_tag(seconds_from_now=7*24*3600)` returns a NIP-40 tag instructing
well-behaved relays to drop the event after the given time.

**Why it matters.** Outbound DMs accumulate on relays forever by default. For a bot
that sends operational messages ("your invoice is ready", "payment received"), there
is no reason for those events to live on public infrastructure for years. Attach a
7-day expiration and relays will garbage-collect them.

**Usage.**

```python
from nostrbot_sdk import expiration_tag
from nostr_sdk import EventBuilder, Kind, Tag

builder = EventBuilder(Kind(4), ciphertext).tags([
    Tag.parse(["p", recipient_pk.to_hex()]),
    expiration_tag(),  # default: 7 days
])
await client.send_event_builder(builder)

# Or a different window:
short_tag = expiration_tag(seconds_from_now=3600)  # 1 hour
```

### Client lifecycle and profile publishing (v0.2.0)

`NostrBot` takes care of: parsing the nsec, building the signer/client, adding all
your relays, connecting, then publishing your bot's kind 0 profile, NIP-65 relay
list (kind 10002), and NIP-17 DM inbox list (kind 10050).

**Why it matters.** Bots that don't publish kind 10050 cannot receive NIP-17 DMs from
modern clients (Damus, Amethyst). Bots that don't publish kind 10002 are invisible to
relay-discovery flows. Doing this right is rote but easy to skip, and the failure
mode is silent (DMs from some users just never arrive).

**Usage.**

```python
cfg = NostrBotConfig(
    nsec="nsec1...",
    relays=["wss://relay.damus.io", "wss://nos.lol", "wss://relay.snort.social"],
    profile={
        "name": "Notible",
        "about": "I do a thing",
        "picture": "https://unsaltedbutter.ai/avatar.png",
        "lud16": "notible@unsaltedbutter.ai",
        "nip05": "notible@unsaltedbutter.ai",
        "website": "https://unsaltedbutter.ai",
    },
    inbox_relay_count=2,  # advertise the first 2 relays as your NIP-17 inbox
)
bot = NostrBot(cfg)
await bot.start()
```

### Subscription filters tuned for the gotchas (v0.2.0)

`NostrBot` subscribes to three filters with the right shape:

- Kind 4 and kind 9735, both with `.since(start_time)` to skip historical events
- Kind 1059 (gift wraps) with `.limit(0)` instead of `.since(now)`

**Why it matters.** NIP-17 randomizes the `created_at` of gift wraps up to two days
in the past to defeat timing correlation. A naive `.since(now)` filter drops every
gift-wrapped DM. Both bots I built before this package shipped with this bug for at
least a week. The library does it right out of the box.

### Decryption, unwrap, and canonical sender identification (v0.2.0)

`NostrBot` decrypts NIP-04 events, unwraps NIP-17 gift wraps, and hands you a
`SenderContext` with the sender's hex pubkey, npub, and last-seen protocol.

**Why it matters.** Sender identification is the foundation of literally everything
else a bot does: rate limiting, authorization, conversation state, billing. Getting
it wrong is a forgery vulnerability. For NIP-17 specifically there is a subtle
distinction between `UnwrappedGift.sender()` (authenticated, signed by the rumor
sender) and `rumor.author()` (also authenticated by the SDK, but easier to
mis-handle in custom code). The library uses `UnwrappedGift.sender()` consistently.

**Usage.**

```python
@bot.on_dm
async def handle_dm(ctx: SenderContext, text: str) -> None:
    print(ctx.sender_hex)       # 64-char hex
    print(ctx.sender_npub)      # bech32 "npub1..."
    print(ctx.protocol)         # "nip04" | "nip17"
    await ctx.reply("hi")       # uses protocol-matched send
```

### Event deduplication (v0.2.0)

`NostrBot` skips any event whose id it has seen in the last 10 minutes (configurable).

**Why it matters.** Relays redeliver. Multiple relays carrying the same event each
deliver it independently. Reconnections replay. Without dedup, your DM handler runs
two, three, sometimes five times per inbound message; if it has side effects (sends
a reply, charges a user, writes to a DB) you get duplicate side effects. The dedup
dict is bounded and self-pruning.

### Content deduplication (v0.2.0)

In addition to event-id dedup, `NostrBot` skips any `(sender_hex, content)` pair seen
within a short window (default 30s).

**Why it matters.** Some clients send the same DM via both NIP-04 and NIP-17 for
backward compatibility, which means two different event ids carrying the same message
arrive within milliseconds. Without content dedup, you process and reply to it twice.
The window is short enough that a real retry from the user (after a failed reply,
say) still gets through.

### Per-user asyncio lock with idle cleanup (v0.2.0)

Conversation state per sender is serialized behind an `asyncio.Lock` that lives in
the bot. Idle locks are reaped after 5 minutes.

**Why it matters.** Two DMs from the same user can be processed concurrently if you
don't lock. For a stateful bot (OTP relay, multi-turn flow, anything that reads then
writes user state) that is a race condition waiting to happen. The combination of
NIP-04 and NIP-17 delivery makes near-simultaneous DMs the common case, not the
rare one. Cleanup matters because a long-running bot otherwise grows one lock per
user it has ever spoken to.

### Protocol-matched reply (v0.2.0)

`ctx.reply(text)` sends back to the user using their preferred protocol. The
underlying pattern already runs in both production bots in `unsaltedbutter.ai`; the
v0.2.0 library work consolidates it into a single method on `SenderContext` so the
~50 lines of `_send_dm` boilerplate go away in both call sites.

**Why it matters.** If a user reaches you via NIP-04 (legacy clients still common),
replying via NIP-17 means they cannot see your reply. If a user reaches you via
NIP-17, replying via NIP-04 leaks metadata they were trying to avoid. The rule is:

1. If the user has DM'd you before, match the protocol of their most recent DM
2. Otherwise (proactive/outbound), check their kind 10050; if found, NIP-17 to
   their inbox relays; if not, NIP-04

**Usage.**

```python
@bot.on_dm
async def handle_dm(ctx: SenderContext, text: str) -> None:
    await ctx.reply("got it")              # matches their protocol

# Proactive (e.g., from a background job, no inbound DM yet):
await bot.send_dm(some_pubkey_hex, "your widget is ready")
```

### Persistent inbox relay pool (v0.2.0)

When the bot sends a NIP-17 DM to a recipient whose inbox relays aren't in the pool,
those relays are added permanently rather than connected ephemerally.

**Why it matters.** The obvious implementation, "connect to recipient's inbox relays,
send, disconnect," leaves a TIME_WAIT TCP socket per send. A busy bot exhausts
ephemeral ports within hours. Keeping the relays in the pool costs a few open
sockets and avoids the problem entirely. We learned this the hard way.

### System push channel (v0.2.0)

`@bot.on_push("backend")` routes inbound DMs from a known backend pubkey to a
separate handler instead of the normal user-DM flow. The pattern exists today in
both production bots (an inline `if sender_hex == vps_bot_pubkey:` branch); v0.2.0
turns it into a first-class concept with named routes.

**Why it matters.** Many bots have a backend (web app, daemon) that pushes events to
the bot over Nostr DMs: "user X paid invoice Y", "send the welcome message to npub Z".
You don't want those JSON payloads going through your user-facing command router and
being treated like user input. Tagging the sender pubkey routes them straight to a
push handler.

**Usage.**

```python
cfg = NostrBotConfig(
    nsec="...",
    relays=[...],
    accept_pushes_from={
        "abcd1234...": "backend",     # tag pubkeys with a label
        "5678ef90...": "monitor",
    },
)

@bot.on_push("backend")
async def handle_backend(payload: dict) -> None:
    if payload["type"] == "payment_received":
        await bot.send_dm(payload["user"], "thanks for paying!")
```

### Identity resolver: kind 0 metadata cache (v0.2.0)

`IdentityResolver` fetches and caches a sender's display name, lud16, picture, and
nip05 from their kind 0 profile event.

**Why it matters.** Most bots want to know more than just a hex pubkey. Greeting
someone by name, paying them via lud16, or showing a profile picture in a generated
UI all require kind 0 lookups. Doing them ad-hoc means redundant relay queries; the
resolver caches per-pubkey with a TTL.

**Usage.**

```python
@bot.on_dm
async def handle_dm(ctx: SenderContext, text: str) -> None:
    identity = await ctx.resolve_identity()
    name = identity.display_name or identity.name or "friend"
    await ctx.reply(f"hi {name}")
```

### LNURL-pay outbound with NIP-57 zap requests (v0.3.0)

`LnurlPayer` resolves a recipient's lud16 to LNURL-pay params, optionally builds a
NIP-57 zap request, calls the callback to get a bolt11, and pays it through your
configured wallet (BTCPay payouts, LND, etc.).

**Why it matters.** Paying creators, refunding users, splitting revenue, donating to
charity: any bot that handles incoming sats often needs to send some out. Doing it
as a *zap* (rather than a plain LNURL payment) means the recipient gets a public
NIP-57 zap receipt on Nostr, which is way better social signaling. The LNURL-pay
flow has three round-trips, fee-tier retries, and several failure modes worth
factoring out.

**Usage.** (Requires `pip install "nostrbot-sdk[lnurl]"`.)

```python
from nostrbot_sdk import LnurlPayer

payer = LnurlPayer(
    bot_keys=keys,
    btcpay_payout_api_key=os.environ["BTCPAY_PAYOUT_API_KEY"],
    btcpay_url="https://btcpay.example.com",
    btcpay_store_id="...",
)

result = await payer.pay(
    lud16="butterbot@unsaltedbutter.ai",
    amount_sats=500,
    zap_target_pubkey=creator_pubkey_hex,  # optional, makes it a NIP-57 zap
    comment="from Notible for content X",
    source_url="https://unsaltedbutter.ai/content/x",
)

if result.status == "completed":
    print(f"paid {result.actual_sats} sats, fee {result.fee_sats}")
```

### Heartbeat callback (v0.2.0)

`@bot.on_heartbeat(interval=300)` registers an async callback that fires every N
seconds with the bot's uptime in seconds.

**Why it matters.** Any bot deployed in production needs a way to say "I'm alive,
here's my version, here's how long I've been up." The library handles the timing and
the shutdown coordination; you decide what to do with the data (POST to your
monitoring API, log it, etc.).

### Graceful shutdown (v0.2.0)

`await bot.run()` installs SIGINT and SIGTERM handlers, cancels background tasks
cleanly, disconnects from relays, and returns.

**Why it matters.** Bots running under launchd or systemd get killed routinely.
Ungraceful shutdown leaves stale connections on relays and can lose in-flight DMs.
The library does the dance for you.

---

## Roadmap

**v0.1.0 (today):**

- `Nip17Support` — kind 10050 detection with TTL cache
- `validate_zap_receipt` / `ValidatedZap` — pure NIP-57 receipt validator
- `expiration_tag` — NIP-40 helper

**v0.2.0 (next):**

- `NostrBot`, `NostrBotConfig` — client lifecycle, profile publishing, subscriptions,
  inbound dispatch, dedup, per-user lock, protocol-matched reply, system push,
  graceful shutdown, heartbeat
- `SenderContext` — sender hex/npub/protocol + `.reply()` + `.resolve_identity()`
- `IdentityResolver` — kind 0 metadata cache

**v0.3.0:**

- `LnurlPayer` — LNURL-pay outbound with optional NIP-57 zap requests
- More examples, optional persistence adapters

---

## Install

```bash
pip install nostrbot-sdk          # core
pip install "nostrbot-sdk[lnurl]" # adds LNURL-pay support (httpx)
```

## Development

```bash
git clone git@github.com:unsaltedbutter-ai/nostrbot-sdk.git
cd nostrbot-sdk
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT
