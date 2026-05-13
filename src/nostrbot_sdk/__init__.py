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

  LNURL-pay (requires the `[lnurl]` extra):
    - LnurlPayer: outbound LNURL-pay with optional NIP-57 zap requests
    - BtcPayWallet: InvoiceWallet adapter for BTCPay Server
    - LnurlPayParams, PayoutResult, FeePolicy: data types
    - InvoiceWallet: Protocol for plugging in non-BTCPay wallets
    - DEFAULT_FEE_POLICY, DEFAULT_ZAP_RELAYS: sensible defaults

  Helpers:
    - expiration_tag: NIP-40 helper (default 7 days)
    - Dedup, UserLockManager: internal building blocks, exposed for advanced use
"""

from nostrbot_sdk.bot import NostrBot, NostrBotConfig
from nostrbot_sdk.context import SenderContext
from nostrbot_sdk.dedup import Dedup
from nostrbot_sdk.expiration import expiration_tag
from nostrbot_sdk.identity import Identity, IdentityResolver
from nostrbot_sdk.locks import UserLockManager
from nostrbot_sdk.nip17_support import Nip17Support
from nostrbot_sdk.zap_verify import ValidatedZap, validate_zap_receipt

# LNURL-pay symbols are imported lazily to keep httpx optional: anything that
# doesn't import the symbols directly works without the [lnurl] extra installed.
try:
    from nostrbot_sdk.lnurl_pay import (
        DEFAULT_FEE_POLICY,
        DEFAULT_ZAP_RELAYS,
        BtcPayWallet,
        FeePolicy,
        InvoiceWallet,
        LnurlPayer,
        LnurlPayParams,
        PayoutResult,
    )
except ImportError:
    # httpx not installed: LnurlPayer etc. unavailable.
    pass

__all__ = [
    "BtcPayWallet",
    "DEFAULT_FEE_POLICY",
    "DEFAULT_ZAP_RELAYS",
    "Dedup",
    "FeePolicy",
    "Identity",
    "IdentityResolver",
    "InvoiceWallet",
    "LnurlPayParams",
    "LnurlPayer",
    "Nip17Support",
    "NostrBot",
    "NostrBotConfig",
    "PayoutResult",
    "SenderContext",
    "UserLockManager",
    "ValidatedZap",
    "expiration_tag",
    "validate_zap_receipt",
]

__version__ = "0.3.0"
