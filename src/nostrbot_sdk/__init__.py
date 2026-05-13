"""nostrbot-sdk: high-level toolkit for building Nostr bots.

Public API:

  Bot runtime:
    - NostrBot, NostrBotConfig: client lifecycle, dispatch, send_dm
    - SenderContext: per-message handle passed to user callbacks

  Caches:
    - IdentityResolver, Identity: kind 0 metadata cache
    - Nip17Support: kind 10050 (NIP-17 inbox) cache

  Validators:
    - validate_zap_receipt, ValidatedZap: NIP-57 receipt validator

  Helpers:
    - expiration_tag: NIP-40 helper (default 7 days)
    - Dedup, UserLockManager: internal building blocks, exposed for advanced use

Planned for v0.3.0:
    - LnurlPayer: outbound LNURL-pay with optional NIP-57 zap requests
"""

from nostrbot_sdk.bot import NostrBot, NostrBotConfig
from nostrbot_sdk.context import SenderContext
from nostrbot_sdk.dedup import Dedup
from nostrbot_sdk.expiration import expiration_tag
from nostrbot_sdk.identity import Identity, IdentityResolver
from nostrbot_sdk.locks import UserLockManager
from nostrbot_sdk.nip17_support import Nip17Support
from nostrbot_sdk.zap_verify import ValidatedZap, validate_zap_receipt

__all__ = [
    "Dedup",
    "Identity",
    "IdentityResolver",
    "Nip17Support",
    "NostrBot",
    "NostrBotConfig",
    "SenderContext",
    "UserLockManager",
    "ValidatedZap",
    "expiration_tag",
    "validate_zap_receipt",
]

__version__ = "0.2.0"
