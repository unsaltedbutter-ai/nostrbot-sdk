"""nostrbot-sdk: high-level toolkit for building Nostr bots.

Public API:

  Bot runtime:
    - NostrBot, NostrBotConfig: client lifecycle, dispatch, send_dm,
      post_note, post_article
    - SenderContext: per-message handle passed to user callbacks

  Publishing:
    - Publisher: one-shot publisher for cron / CLI scripts
    - PublishResult: outcome of a publish (event_id, note_id, success_relays,
      failed_relays, kind, .ok)
    - send_note, send_article: lower-level helpers (take a Client directly)
    - build_note_tags, build_article_tags, normalize_hashtag: tag builders

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
from nostrbot_sdk.publishing import (
    PublishResult,
    Publisher,
    build_article_tags,
    build_note_tags,
    normalize_hashtag,
    send_article,
    send_note,
)
from nostrbot_sdk.zap_verify import ValidatedZap, validate_zap_receipt

# LNURL-pay symbols are imported lazily to keep httpx optional.
try:
    from nostrbot_sdk.lnurl_pay import (
        DEFAULT_FEE_POLICY,
        DEFAULT_ZAP_RELAYS,
        BtcPayWallet,
        FeePolicy,
        InvoiceWallet,
        LnurlPayer,
        LnurlPayParams,
        LnurlSecurityError,
        PaymentOutcomeUnknown,
        PayoutResult,
    )
except ImportError:
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
    "LnurlSecurityError",
    "Nip17Support",
    "NostrBot",
    "NostrBotConfig",
    "PaymentOutcomeUnknown",
    "PayoutResult",
    "PublishResult",
    "Publisher",
    "SenderContext",
    "UserLockManager",
    "ValidatedZap",
    "build_article_tags",
    "build_note_tags",
    "expiration_tag",
    "normalize_hashtag",
    "send_article",
    "send_note",
    "validate_zap_receipt",
]

__version__ = "0.5.0"
