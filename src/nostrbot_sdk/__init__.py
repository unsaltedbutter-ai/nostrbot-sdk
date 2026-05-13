"""nostrbot-sdk: high-level toolkit for building Nostr bots.

Currently exported (v0.1.0):
  - Nip17Support: kind 10050 detection + TTL cache
  - validate_zap_receipt, ValidatedZap: pure NIP-57 receipt validator
  - expiration_tag: NIP-40 helper (default 7 days)

Planned for upcoming releases:
  - NostrBot, NostrBotConfig: client lifecycle + event dispatch
  - DMRouter, ZapRouter, SenderContext: routing primitives
  - IdentityResolver: kind 0 metadata cache (name, lud16)
  - LnurlPayer: outbound LNURL-pay with NIP-57 zap requests
"""

from nostrbot_sdk.expiration import expiration_tag
from nostrbot_sdk.nip17_support import Nip17Support
from nostrbot_sdk.zap_verify import ValidatedZap, validate_zap_receipt

__all__ = [
    "Nip17Support",
    "ValidatedZap",
    "expiration_tag",
    "validate_zap_receipt",
]

__version__ = "0.1.0"
