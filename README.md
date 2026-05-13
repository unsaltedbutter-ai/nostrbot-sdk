# nostrbot-sdk

High-level toolkit for building Nostr bots in Python: DM listening (NIP-04 + NIP-17),
protocol-matched replies, zap receipt validation (NIP-57), sender identification, and
LNURL-pay outbound zaps. Built on [`nostr-sdk`](https://pypi.org/project/nostr-sdk/).

The goal: write a Nostr bot in 50 lines instead of 500.

```python
from nostrbot_sdk import NostrBot, NostrBotConfig, ValidatedZap, SenderContext

cfg = NostrBotConfig(
    nsec="nsec1...",
    relays=["wss://relay.damus.io", "wss://nos.lol"],
    profile={"name": "MyBot", "about": "an example bot"},
    zap_provider_pubkey="<hex of your LNURL provider's nostr pubkey>",
)

bot = NostrBot(cfg)

@bot.on_dm
async def handle_dm(ctx: SenderContext, text: str) -> None:
    await ctx.reply(f"echo: {text}")

@bot.on_zap
async def handle_zap(zap: ValidatedZap, ctx: SenderContext) -> None:
    await ctx.reply(f"thanks for the {zap.amount_sats} sats")

await bot.run()
```

## What it handles for you

- Connect + manage relays, publish profile (kind 0), NIP-65 relay list (kind 10002),
  NIP-17 DM inbox list (kind 10050)
- Three subscriptions tuned correctly (kind 4 + 9735 with `.since(now)`, kind 1059
  with `.limit(0)` — gift wraps randomize timestamps so `.since` drops them)
- Decrypt NIP-04, unwrap NIP-17 gift wraps, canonicalize sender identification
- Event dedup (relays redeliver), content dedup (same DM via both NIP-04 and NIP-17)
- Per-user `asyncio.Lock` to serialize conversation state, with idle cleanup
- Protocol-matched replies: NIP-04 in → NIP-04 out; NIP-17 in → NIP-17 out;
  proactive sends check kind 10050 and route through the recipient's inbox relays
- NIP-40 expiration tags (7 days, configurable)
- Persistent inbox relay pool (avoid TIME_WAIT socket leaks from connect/disconnect)
- NIP-57 zap receipt validation (all 6 checks)
- System push channel: route DMs from a known backend pubkey to a separate handler
- Kind 0 metadata cache (display name, lud16) for sender enrichment
- Optional LNURL-pay outbound (creator payouts, tips) with NIP-57 zap requests
- Graceful shutdown on SIGINT/SIGTERM, periodic heartbeat callback

## Status

Pre-1.0. API may change. Pinned to `nostr-sdk==0.44.2`.

## License

MIT
