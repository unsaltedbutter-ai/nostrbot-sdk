"""SenderContext: per-message handle passed to user callbacks.

When a DM arrives or a zap receipt validates, NostrBot constructs a
SenderContext for the originating account and passes it to the registered
handler. The context exposes the sender's hex pubkey, bech32 npub, and the
protocol the message arrived on, plus convenience methods for the two
things handlers do most often: reply and resolve identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from nostrbot_sdk.bot import NostrBot
    from nostrbot_sdk.identity import Identity


Protocol = Literal["nip04", "nip17", "zap"]


@dataclass
class SenderContext:
    """Handler context for a single inbound DM or zap.

    Fields:
        sender_hex: 64-char hex pubkey of the originator.
        sender_npub: bech32 "npub1..." form of the same pubkey.
        protocol: how the message arrived. "nip04" or "nip17" for DMs;
            "zap" for zap receipts (where there is no inbound DM and any
            reply uses the protocol-detection fallback via kind 10050).

    Methods:
        reply(text): send a DM back to this sender using the protocol they
            used. For zap contexts, falls back to kind 10050 detection.
        resolve_identity(): fetch and cache this sender's kind 0 metadata.
    """

    sender_hex: str
    sender_npub: str
    protocol: Protocol
    _bot: NostrBot

    async def reply(self, text: str) -> None:
        """Send a DM back using the protocol this sender used.

        For DM contexts (nip04/nip17), the bot already knows the protocol
        from the inbound event and reuses it. For zap contexts the bot has
        no inbound DM to match against, so it consults kind 10050 and falls
        back to NIP-04 if the recipient hasn't published one.
        """
        await self._bot.send_dm(self.sender_hex, text)

    async def resolve_identity(self) -> Identity:
        """Fetch this sender's kind 0 metadata (cached)."""
        return await self._bot.identity.resolve(self.sender_hex)
