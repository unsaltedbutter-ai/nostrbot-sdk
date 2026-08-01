# nostrbot-sdk

High-level toolkit for building Nostr bots in Python. Handles the protocol plumbing
(DM listening, NIP-17 gift wraps, zap receipt validation, note publishing, LNURL-pay)
so your bot code can stay focused on what it actually does.

Built on [`nostr-sdk`](https://pypi.org/project/nostr-sdk/) (`>=0.45.0a7,<0.46`).

**Status:** **v0.5.2 shipped.** The full runtime, LNURL-pay, and publishing are all
callable today. 213 tests, MIT licensed. Each feature section below carries a
`(vX.Y.Z)` tag indicating the minimum version it landed in; pin to that or later.
See [Versions](#versions) for the shipped-when changelog.

## Why this over direct nostr-sdk?

Every Nostr bot I wrote before this package shipped with the same 400-1000 lines of
boilerplate, and most of them had at least one of these footguns:

| Footgun | What goes wrong without the library |
|---|---|
| Kind 1059 with `.since(now)` | NIP-17 randomizes `created_at` up to 2 days back, so every gift-wrapped DM gets dropped silently. |
| Per-call connect/disconnect | TIME_WAIT TCP sockets accumulate; busy bots exhaust ephemeral ports. |
| Trusting zap receipts | Kind 9735 is unauthenticated by default. Skip any of the six NIP-57 checks and you've shipped a free-money exploit. |
| Replying via the wrong protocol | NIP-04 in / NIP-17 out means the user never sees your reply; NIP-17 in / NIP-04 out leaks metadata. |
| Same DM via NIP-04 and NIP-17 | Many clients send both for compat. Without content dedup you process and reply twice. |
| Reply threading | NIP-10 marked e-tags (`"root"`, `"reply"`) are subtle; positional mode is legacy. Mess this up and your reply lands as a top-level post. |
| Forgetting kind 10050 | Bots that don't publish a DM inbox list silently can't receive NIP-17 from modern clients. |
| LNURL-pay fee escalation | Without graduated retries, a fee-inflating route either fails the payment or pays the routing node more than the creator. |

Each one is fixable in a few lines once you know about it. This package factors them
out so you don't rediscover them per project.

## Quickstart

Handle DMs, validate zaps, route system pushes, and publish notes from one process:

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
    accept_pushes_from={"<vps_bot_hex>": "vps"},  # optional
)

bot = NostrBot(cfg)

@bot.on_dm
async def handle_dm(ctx: SenderContext, text: str) -> None:
    await ctx.reply(f"echo: {text}")

@bot.on_zap
async def handle_zap(zap: ValidatedZap, ctx: SenderContext) -> None:
    await ctx.reply(f"thanks for the {zap.amount_sats:,} sats")

@bot.on_push("vps")  # JSON pushes from a known backend pubkey
async def handle_push(raw: str) -> None:
    ...

@bot.on_heartbeat(interval=300)
async def heartbeat(uptime_s: int) -> None:
    ...

await bot.run()  # SIGINT/SIGTERM hooked up; blocks until shutdown
```

Notes are published with the bot's already-connected client, so `post_note`
needs a *started* bot — call it from inside a handler, or drive the lifecycle
yourself:

```python
await bot.start()
result = await bot.post_note("hello nostr", hashtags=["nostr", "introductions"])
if result.ok:
    print(f"published to {result.relay_count} relays: {result.note_id}")
...
await bot.stop()
```

For one-shot scripts (cron, CLI tools) that only need to publish, use `Publisher`:

```python
from nostrbot_sdk import Publisher

async with Publisher.from_nsec(nsec, relays) as pub:
    result = await pub.post_note("daily digest", hashtags=["digest"])
```

---

## Features

Each section below describes the problem the feature solves and how to use it. The
`(vX.Y.Z)` tag on each section is the minimum version that needs to be installed for
the API to be available; everything through v0.4.0 is shipped today.

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
from nostr_sdk import RelayUrl, SendEventTarget, nip17_make_private_msg_async

detector = Nip17Support(client, ttl_seconds=3600)

dm_relays = await detector.check(recipient_pubkey_hex)
if dm_relays is not None:
    relay_urls = [RelayUrl.parse(r) for r in dm_relays]
    wrap = await nip17_make_private_msg_async(signer, pk, "hello")
    await client.send_event(wrap, target=SendEventTarget.to(relay_urls))
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

### NIP-40 expiration tag helpers (v0.1.0, gift-wrap support in v0.5.2)

`expiration_tag(seconds_from_now=7*24*3600)` returns a NIP-40 tag instructing
well-behaved relays to drop the event after the given time.
`dm_expiration_tag(...)` is its gift-wrap-aware sibling.

**Why it matters.** Outbound DMs accumulate on relays forever by default. For a bot
that sends operational messages ("your invoice is ready", "payment received"), there
is no reason for those events to live on public infrastructure for years. Attach a
7-day expiration and relays will garbage-collect them.

For NIP-17 the tag has to go in **two** places. A relay only ever sees the outer
gift wrap — it cannot read the rumor — so an expiration that lives only inside is
invisible to it and nothing is ever collected. Put it only on the wrap and the
recipient's client has no idea the message is meant to expire. `NostrBot.send_dm`
stamps the same tag on both, so the two never disagree.

**Why the timestamp is backdated.** A gift wrap's `created_at` is deliberately
randomized up to two days into the past so observers can't tell when a message was
really sent. A plain `now + 7d` expiration would hand that straight back —
`expiration - 7d` is the real send time, and 7 days is a guessable default.
`dm_expiration_tag` therefore anchors to `now - r` for an unpredictable `r` drawn
from `[0, min(2 days, expiry/4)]`: at least three quarters of the configured
lifetime survives, the wrap is never born already expired, and the send time stays
hidden. It logs a warning for expiries at or below the 2-day window, where timing
privacy is necessarily weaker.

**Usage.**

```python
from nostrbot_sdk import dm_expiration_tag, expiration_tag
from nostr_sdk import EventBuilder, Kind, Tag, nip59_make_gift_wrap_async

# Plain events (kind 4, kind 1, ...): created_at is already the real send time.
event = await (
    EventBuilder(Kind(4), ciphertext)
    .tags([Tag.public_key(recipient_pk), expiration_tag()])  # default: 7 days
    .finalize_async(signer)
)
await client.send_event(event)

# Gift-wrapped NIP-17: one tag, stamped inside AND outside.
exp = dm_expiration_tag()
rumor = (
    EventBuilder(Kind(14), "hello")
    .tags([Tag.public_key(recipient_pk), exp])
    .finalize_unsigned(my_pubkey)
)
wrap = await nip59_make_gift_wrap_async(
    signer, recipient_pk, rumor, expiration=None, extra_tags=[exp],
)
await client.send_event(wrap)

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

Zap receipts get a second, much longer replay window (24h by default,
`zap_dedup_ttl_seconds`): a malicious relay redelivering a valid kind 9735 after
the 10-minute event dedup expires must not credit the sender twice. This guard is
in-memory — zap handlers that credit balances should also persist processed event
ids so replays across bot restarts are rejected too.

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

`ctx.reply(text)` sends back to the user using their preferred protocol. One method
call replaces ~50 lines of per-bot `_send_dm` boilerplate (NIP-04 encode + p-tag +
expiration vs building and sending a gift wrap, plus the kind 10050 lookup for proactive
sends).

**Why it matters.** If a user reaches you via NIP-04 (legacy clients still common),
replying via NIP-17 means they cannot see your reply. If a user reaches you via
NIP-17, replying via NIP-04 leaks metadata they were trying to avoid. And NIP-17
replies must go to the *recipient's* kind 10050 inbox relays — broadcasting the
gift wrap to your own relay pool only works if the user happens to read from it.
The rule is:

1. If the user's most recent DM was NIP-04, reply via NIP-04
2. Otherwise (NIP-17 replier, or proactive/outbound), check their kind 10050;
   if found, NIP-17 to their inbox relays; if they DM'd you via NIP-17 but no
   10050 is findable right now, best-effort NIP-17 to your own pool; else NIP-04

`send_dm` / `ctx.reply` return `True` on success and `False` on failure (errors
are logged, never raised).

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
separate handler instead of the normal user-DM flow. Replaces the inline
`if sender_hex == vps_bot_pubkey:` branch with a named route, and any DM from a
sender NOT in `accept_pushes_from` still flows through `on_dm` as normal.

**Why it matters.** Many bots have a backend (web app, daemon) that pushes events to
the bot over Nostr DMs: "user X paid invoice Y", "send the welcome message to npub Z".
You don't want those JSON payloads going through your user-facing command router and
being treated like user input. Tagging the sender pubkey routes them straight to a
push handler.

**Usage.**

```python
import json

cfg = NostrBotConfig(
    nsec="...",
    relays=[...],
    accept_pushes_from={
        "abcd1234...": "backend",     # tag pubkeys with a label
        "5678ef90...": "monitor",
    },
)

@bot.on_push("backend")
async def handle_backend(raw: str) -> None:
    # Push handlers receive the raw decrypted plaintext; parse it yourself.
    payload = json.loads(raw)
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
signed NIP-57 zap request so the recipient gets a public zap receipt, requests a
bolt11 from the LNURL callback, and pays it via a swappable `InvoiceWallet`.

**Why it matters.** Paying creators, refunding users, splitting revenue, donating to
charity: any bot that handles incoming sats often needs to send some out. Doing it
*as a zap* (rather than a plain LNURL payment) means the recipient gets a public
NIP-57 zap receipt on Nostr, which is much better social signaling. The library
also handles **graduated fee-tier retries** so a fee-inflating routing node can't
either fail the payment or extract value from your margin — total outflow per
payment is capped at `amount_sats + operator_contribution_sats` (default 2).

Two safety properties on top of that:

- **Invoice verification (LUD-06).** The bolt11 returned by the LNURL callback
  is decoded and its amount must equal what was requested, otherwise
  `LnurlSecurityError` is raised and nothing is paid — a malicious server can't
  hand you an invoice for an arbitrary amount. For zaps, the invoice's
  `description_hash` must also commit to the zap request; if it doesn't, the
  payment proceeds but is treated as plain LNURL (no fake zap receipt claim).
- **No retry on unknown outcome.** If a wallet call times out or the backend
  reports the payment as pending, the Lightning payment may still settle.
  `PaymentOutcomeUnknown` is raised and **no further fee tier is tried** —
  paying a fresh invoice at that point could double-pay. Reconcile with your
  node before retrying that payout. Custom `InvoiceWallet` implementations
  should raise `PaymentOutcomeUnknown` for timeouts/pending and plain errors
  only for definitive failures.

**Usage.** Requires `pip install "nostrbot-sdk[lnurl]"`.

```python
import os
from nostrbot_sdk import LnurlPayer, BtcPayWallet

wallet = BtcPayWallet(
    url="https://btcpay.example.com",
    store_id="...",
    api_key=os.environ["BTCPAY_PAYOUT_API_KEY"],
)
payer = LnurlPayer(keys=bot.keys, wallet=wallet)

result = await payer.pay(
    lud16="butterbot@unsaltedbutter.ai",
    amount_sats=500,
    zap_target_pubkey=creator_pubkey_hex,  # optional: makes it a NIP-57 zap
    comment="from Notible for content X",
    source_url="https://unsaltedbutter.ai/content/x",  # appended to default comment
)

if result.status == "paid":
    print(f"paid {result.actual_sats} sats, fee {result.fee_sats}")
elif result.status == "skipped":
    print("amount outside the recipient's min/max sendable range")
```

`BtcPayWallet` is one concrete implementation; any class implementing the
`InvoiceWallet` protocol (a single async `pay(bolt11, max_fee_percent) -> (total_sats,
fee_sats)` method) plugs in. Custom fee policy:

```python
from nostrbot_sdk import LnurlPayer, FeePolicy

payer = LnurlPayer(
    keys=bot.keys,
    wallet=wallet,
    fee_policy=FeePolicy(
        operator_contribution_sats=5,
        fee_budgets_sats=(1, 5, 20),  # only try three tiers
    ),
)
```

### Publishing notes (v0.4.0)

`bot.post_note(content, ...)` publishes a kind 1 note using the bot's already-
connected client. Returns a `PublishResult` so you can detect partial / total
publish failure instead of inferring from log lines.

**Why it matters.** A bare `EventBuilder(Kind(1), ...).finalize_async(signer)` +
`client.send_event(...)`
call works for the simplest case, but the moment you want to **reply**, **quote**,
**add hashtags**, **set an expiration**, or **know whether any relay actually
accepted the event**, you're rebuilding the same plumbing. Three specific gotchas:

- **NIP-10 reply threading**: replies need an `e`-tag marked `"root"` for the
  thread root and (separately) `"reply"` for the immediate parent, plus a
  `p`-tag for the parent author (or they never see your reply in their
  notifications). Get this wrong and your reply shows up as a top-level post,
  not under the parent. The library takes `reply_to=<parent_id>`, optionally
  `reply_root=<root_id>`, and `reply_to_author=<hex>`; emits the right tags.
- **NIP-18 quoting**: requires a `q`-tag for the quoted event PLUS a `p`-tag for
  the quoted author (so they see it in notifications). Library does both from
  `quote=<id>` + `quote_author=<hex>`.
- **NIP-12 hashtag normalization**: `t`-tags should be lowercased, with leading
  `#` stripped. Library does this and dedups.

**Usage:**

```python
# Basic note with hashtags and a 7-day expiration:
result = await bot.post_note(
    "GM nostr",
    hashtags=["#GM", "introductions"],   # normalized to "gm", "introductions"
    expiration_seconds=7 * 24 * 3600,
)
print(result.ok, result.note_id, result.success_relays)
# True  note1...  ["wss://relay.damus.io/", ...]

# Reply to a thread (NIP-10 marked mode):
await bot.post_note(
    "good question",
    reply_to=parent_event_id_hex,
    reply_root=thread_root_event_id_hex,   # omit if reply_to IS the root
    reply_to_author=parent_author_hex,     # p-tag so the author is notified
)

# Quote another note (NIP-18):
await bot.post_note(
    "this is the post of the year",
    quote=other_event_id_hex,
    quote_author=other_author_pubkey_hex,
)

# Mention several users:
await bot.post_note(
    "thanks to nostr:npub1... and nostr:npub1...",
    mention_pubkeys=[alice_hex, bob_hex],
)
```

`PublishResult` fields: `event_id` (64-char hex), `note_id` (bech32 `note1...`),
`success_relays`, `failed_relays` (relay → error message), `kind`, and the
`.ok` property which is `True` iff at least one relay accepted the event.

### Publishing long-form articles (NIP-23) (v0.4.0)

`bot.post_article(title, content, identifier=...)` publishes a kind 30023 article
with all the NIP-23 tags baked in. The `identifier` is the `d`-tag value; publishing
again with the same identifier replaces the prior version (addressable events).

**Why it matters.** Long-form articles need a specific tag set: `d` (unique
identifier per author), `title`, `summary`, `image`, `published_at` (Unix
timestamp), `t` (hashtags). It's easy to forget one and end up with a
half-rendered article in Habla / Yakihonne / Highlighter. The library builds the
full tag set and validates that `identifier` is non-empty.

**Usage:**

```python
result = await bot.post_article(
    title="On the design of Nostr bots",
    content="# Markdown body\n\nFirst paragraph...",
    identifier="nostr-bot-design",           # required
    summary="A look at the protocol footguns...",
    image="https://unsaltedbutter.ai/articles/cover.png",
    hashtags=["nostr", "bots"],
    published_at=1715616000,                 # Unix timestamp
)
```

To edit the article, call `post_article` again with the same `identifier`; relays
that honor NIP-23 will replace the prior version.

### One-shot publishing for cron scripts (v0.4.0)

`Publisher` is a minimal async-context-manager that holds one Client + Keys for the
lifetime of a script. Use it when you don't need the full `NostrBot` runtime (no
DMs, no zaps, no event loop) — just publishing.

**Why it matters.** A naive `Client()` + relay setup per cron invocation
adds + connects + disconnects relays every time. If you run hourly, that's 24
connect/disconnect cycles per day per script. `Publisher` lets you open and close
the client once around the entire publish set, and exposes the underlying client
via `.client` if you need to fetch events too.

**Usage:**

```python
from nostrbot_sdk import Publisher

async with Publisher.from_nsec(nsec, relays) as pub:
    result = await pub.post_note(
        "Daily digest\n\n▸ ...",
        hashtags=["digest"],
    )
    if not result.ok:
        print(f"publish failed: {result.failed_relays}")
        # All relays rejected; alert someone.

    # Need to fetch events too? .client is exposed:
    events = await pub.client.fetch_events(some_filter, timeout)
```

For more complex flows where you've manually constructed `Tag` objects (mention
markers, NIP-10 positional-mode replies, etc.), pass them via `extra_tags=`:

```python
from nostr_sdk import Tag

await pub.post_note(
    "see this thread:",
    extra_tags=[Tag.parse(["e", root_id, "", "mention"])],  # legacy mention marker
)
```

### Tag builders (v0.4.0)

If you need the assembled tag list without publishing (e.g., for a dry-run preview,
or to merge with other tags before sending), the tag builders are exposed
separately:

```python
from nostrbot_sdk import build_note_tags, build_article_tags, normalize_hashtag

tags = build_note_tags(
    reply_to=parent_id, reply_root=root_id,
    hashtags=["nostr"], expiration_seconds=3600,
)
# -> list[Tag]

article_tags = build_article_tags(
    identifier="my-article", title="Title", hashtags=["#Nostr"],
)

normalize_hashtag("  #Bitcoin  ")  # -> "bitcoin"
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

## Versions

- **v0.5.2** (current) — NIP-40 expiration on gift wraps; nostr-sdk 0.45 upgrade.
  - **Gift-wrapped DMs now actually expire.** The NIP-40 tag was only ever
    placed inside the rumor, which a relay cannot read — so nothing was
    garbage-collected and gift wraps accumulated forever. `send_dm` now stamps
    the *same* expiration on both the rumor and the outer wrap, so the
    recipient and the relay agree on when the message dies.
  - **The shared timestamp is backdated** (new `dm_expiration_tag`) to
    `now - r` for r in `[0, min(2 days, expiry/4)]`. A plain `now + 7d` would
    let any observer recover the real send time as `expiration - 7d` and undo
    the `created_at` randomization NIP-59 exists to provide. At least three
    quarters of the configured lifetime always survives, and the wrap is never
    created already expired. Expiries at or below the 2-day gift-wrap window
    log a warning.
  - **Dependency**: `nostr-sdk` moved from `==0.44.2` to `>=0.45.0a7,<0.46`
    (0.45 is where the expiration API landed). Breaking upstream changes
    absorbed: `Client` no longer holds a signer, `send_event_builder` /
    `send_private_msg*` / `set_metadata` / `fetch_metadata` / `TagKind` /
    the `HandleNotification` trait are all gone, filters are wrapped in
    `ReqTarget`, and notifications are a pull stream.
  - **Breaking (ours)**: `send_note` / `send_article` take a required
    `signer=` keyword — the client can no longer sign on their behalf.
    `NostrBot.post_note` / `Publisher.post_note` are unaffected.
- **v0.5.1** — lifecycle fix.
  - `run()` no longer re-calls `start()` on an already-started bot, so the
    `await bot.start()` → publish → `await bot.run()` sequence works instead
    of raising `RuntimeError` after the bot is already connected. A genuine
    double-`start()` still fails loudly.
- **v0.5.0** — correctness, safety, and lifecycle hardening.
  - **LNURL-pay safety**: bolt11 invoices from the callback are decoded and
    verified (amount must match the request; zap invoices must commit to the
    zap request via `description_hash`). New `LnurlSecurityError`. Wallet
    failures are now classified: new `PaymentOutcomeUnknown` (timeout / 5xx /
    pending) aborts fee-tier retries so an in-flight payment can't be
    double-paid; `BtcPayWallet` checks the BTCPay `status` field.
  - **Dedup TTL enforced on lookup**: an expired entry reads as new even
    before pruning runs — low-traffic bots no longer drop legitimate retries.
  - **NIP-17 reply delivery**: replies to NIP-17 senders now go to the
    recipient's kind 10050 inbox relays (previously broadcast to the bot's
    own pool, which the recipient may never read).
  - **Zap replay guard**: validated receipt ids remembered for 24h
    (`zap_dedup_ttl_seconds`), closing a replay-after-dedup-expiry
    double-credit window.
  - **Lifecycle**: handler tasks are strongly referenced (no GC'd in-flight
    DMs) and drained on stop() with `shutdown_grace_seconds`; notification
    consumer starts before subscribing; bots are restartable after stop().
  - **Caches**: transient fetch failures are no longer negative-cached by
    `Nip17Support` / `IdentityResolver`; stale entries are served instead.
  - **Memory**: per-user protocol hints expire (`user_protocol_ttl_seconds`).
  - **Push channel**: content dedup applied (dual NIP-04/NIP-17 backend
    sends no longer double-process).
  - **NIP-10**: new `reply_to_author` param adds the parent author's p-tag.
  - `send_dm` / `ctx.reply` now return `bool` instead of silently swallowing
    failures.
- **v0.4.0** — publishing.
  - `NostrBot.post_note` / `NostrBot.post_article` on a running bot.
  - `Publisher` (async context manager) for one-shot cron / CLI scripts.
  - `PublishResult` with `.ok`, `success_relays`, `failed_relays`, `event_id`,
    `note_id`, `kind`.
  - Tag builders: `build_note_tags`, `build_article_tags`, `normalize_hashtag`.
  - Hides NIP-10 reply threading (root/reply markers), NIP-18 quoting (q+p tags),
    NIP-23 long-form tag set, NIP-12 hashtag normalization, NIP-40 expiration,
    and Client TIME_WAIT churn from per-call connect/disconnect.
- **v0.3.0** — LNURL-pay.
  - `LnurlPayer`, `BtcPayWallet`, `InvoiceWallet` protocol, `FeePolicy`,
    `PayoutResult`, `DEFAULT_FEE_POLICY`, `DEFAULT_ZAP_RELAYS`.
  - NIP-57 zap requests with graduated fee-tier retries; total outflow bounded
    at `amount_sats + operator_contribution_sats`.
  - Helpers also exposed: `resolve_lud16`, `create_zap_request`, `request_invoice`.
  - `httpx` is in the `[lnurl]` optional extra.
- **v0.2.0** — bot runtime.
  - `NostrBot`, `NostrBotConfig`, `SenderContext`.
  - `IdentityResolver` (kind 0 cache), `Identity`.
  - Client lifecycle, profile publishing (kind 0 / 10002 / 10050), three
    correctly-tuned subscriptions, NIP-04 + NIP-17 dispatch, event + content
    dedup, per-user `asyncio.Lock` with idle cleanup, protocol-matched
    `send_dm`, persistent inbox relay pool, system push channel, heartbeat,
    graceful shutdown.
  - Internal building blocks also exposed for advanced use: `Dedup`,
    `UserLockManager`.
- **v0.1.0** — primitives.
  - `Nip17Support` — kind 10050 detection with TTL cache.
  - `validate_zap_receipt` / `ValidatedZap` — pure NIP-57 receipt validator.
  - `expiration_tag` — NIP-40 helper.

---

## Install

Not on PyPI yet. Install from the public GitHub repo, pinned to a tag:

```bash
# Core (NostrBot, publishing, primitives):
pip install "nostrbot-sdk @ git+https://github.com/unsaltedbutter-ai/nostrbot-sdk.git@v0.5.1"

# With LNURL-pay (adds httpx):
pip install "nostrbot-sdk[lnurl] @ git+https://github.com/unsaltedbutter-ai/nostrbot-sdk.git@v0.5.1"
```

In a `requirements.txt`:

```text
nostrbot-sdk[lnurl] @ git+https://github.com/unsaltedbutter-ai/nostrbot-sdk.git@v0.5.1
```

## Development

```bash
git clone git@github.com:unsaltedbutter-ai/nostrbot-sdk.git
cd nostrbot-sdk
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[lnurl,dev]"
pytest
```

## License

MIT
