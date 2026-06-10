"""LNURL-pay outbound with optional NIP-57 zap requests.

Resolves a Lightning address (lud16), optionally builds a NIP-57 zap
request so the recipient gets a Nostr-visible zap, requests a bolt11
invoice from the recipient's LNURL provider, and pays it via a swappable
`InvoiceWallet`.

Includes a graduated fee-tier retry policy: early tiers send the full
creator share with a tight fee cap (the operator absorbs the routing fee
from their margin); later tiers reduce the creator payment so total
outflow stays bounded. This prevents creators from extracting excess
value via routing-node fee manipulation.

Requires the `[lnurl]` extra (`pip install "nostrbot-sdk[lnurl]"`) for httpx.

Usage:

    from nostrbot_sdk import LnurlPayer, BtcPayWallet

    wallet = BtcPayWallet(
        url="https://btcpay.example.com",
        store_id="...",
        api_key=os.environ["BTCPAY_API_KEY"],
    )
    payer = LnurlPayer(keys=bot.keys, wallet=wallet)

    result = await payer.pay(
        lud16="butterbot@unsaltedbutter.ai",
        amount_sats=500,
        zap_target_pubkey=author_hex,        # optional: makes it a NIP-57 zap
        comment="optional comment",
        source_url="https://example.com/x",  # optional: included in default comment
    )
"""

from __future__ import annotations

import hashlib
import logging
import math
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import bolt11 as bolt11_lib

try:
    import httpx
except ImportError as e:
    raise ImportError(
        "nostrbot-sdk.lnurl_pay requires httpx. "
        "Install with: pip install 'nostrbot-sdk[lnurl]'"
    ) from e

from nostr_sdk import EventBuilder, Kind, PublicKey, Tag, TagKind

if TYPE_CHECKING:
    from nostr_sdk import Keys

log = logging.getLogger(__name__)


# Default Nostr relays to advertise in NIP-57 zap requests. Override per-instance
# if you want different relays.
DEFAULT_ZAP_RELAYS: list[str] = [
    "wss://nos.lol",
    "wss://relay.damus.io",
    "wss://relay.snort.social",
]


class LnurlSecurityError(Exception):
    """The LNURL server returned an invoice that doesn't match the request.

    Raised when the bolt11 from the callback is undecodable or carries a
    different amount than was requested. Paying such an invoice would send
    funds the caller never approved, so this aborts the payment outright
    instead of retrying at another fee tier.
    """


class PaymentOutcomeUnknown(Exception):
    """The wallet could not determine whether the payment settled.

    Raised on timeouts, dropped connections, 5xx responses, or a payment
    the backend reports as still pending. The Lightning payment may still
    settle after this is raised. Callers MUST NOT pay another invoice for
    the same payout until they have reconciled with the wallet/node —
    retrying risks paying twice.
    """


@dataclass(frozen=True)
class LnurlPayParams:
    """Parsed response from an LNURL-pay endpoint."""

    callback: str
    min_sendable: int       # msats
    max_sendable: int       # msats
    comment_allowed: int    # max comment length (0 = unsupported)
    allows_nostr: bool
    nostr_pubkey: str | None  # recipient's LNURL provider pubkey (hex)


@dataclass(frozen=True)
class PayoutResult:
    """Outcome of a LnurlPayer.pay() call."""

    status: str  # "paid", "failed", "skipped"
    fee_sats: int | None = None
    actual_sats: int | None = None


# -- Fee tier policy -----------------------------------------------------------


@dataclass(frozen=True)
class FeePolicy:
    """Graduated fee-budget retry policy.

    `operator_contribution_sats`: how many sats the operator funds out of
    their margin before each additional sat of routing fee starts coming
    out of the creator's share. Total outflow per payment is capped at
    `amount_sats + operator_contribution_sats`.

    `fee_budgets_sats`: ordered list of fee budgets to try in turn. Each
    budget produces a (creator_amount, fee_cap_pct) tuple; the first
    that succeeds wins.
    """

    operator_contribution_sats: int = 2
    fee_budgets_sats: tuple[int, ...] = (1, 2, 3, 4, 5, 7, 10)

    def tiers(self, amount_sats: int) -> list[tuple[int, float]]:
        """Compute (creator_sats, max_fee_pct) for each fee budget tier.

        Early tiers send the full creator share with a tight fee cap (funded
        by the operator's contribution). Later tiers reduce the creator
        payment by 1 sat per additional sat of fee budget, keeping total
        outflow bounded.
        """
        max_outflow = amount_sats + self.operator_contribution_sats
        result: list[tuple[int, float]] = []
        for fee_budget in self.fee_budgets_sats:
            if fee_budget <= self.operator_contribution_sats:
                creator_sats = amount_sats
            else:
                creator_sats = max_outflow - fee_budget
            if creator_sats < 1:
                break
            fee_pct = fee_budget / creator_sats * 100
            result.append((creator_sats, fee_pct))
        return result


DEFAULT_FEE_POLICY = FeePolicy()


# -- LNURL helpers -------------------------------------------------------------


async def resolve_lud16(lud16: str, timeout: float = 15.0) -> LnurlPayParams:
    """Fetch the LNURL-pay params from https://<domain>/.well-known/lnurlp/<user>.

    Raises ValueError on malformed lud16 or LNURL error response.
    """
    parts = lud16.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid lud16: {lud16}")

    user, domain = parts
    url = f"https://{domain}/.well-known/lnurlp/{user}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") == "ERROR":
        raise ValueError(f"LNURL-pay error for {lud16}: {data.get('reason')}")

    if (
        not data.get("callback")
        or not data.get("minSendable")
        or not data.get("maxSendable")
    ):
        raise ValueError(f"Invalid LNURL-pay response for {lud16}")

    return LnurlPayParams(
        callback=data["callback"],
        min_sendable=data["minSendable"],
        max_sendable=data["maxSendable"],
        comment_allowed=data.get("commentAllowed", 0),
        allows_nostr=data.get("allowsNostr") is True,
        nostr_pubkey=(
            data.get("nostrPubkey")
            if isinstance(data.get("nostrPubkey"), str)
            else None
        ),
    )


def create_zap_request(
    recipient_hex: str,
    amount_msats: int,
    relays: list[str],
    comment: str,
    bot_keys: Keys,
) -> str:
    """Create a signed NIP-57 kind 9734 zap request, return JSON string.

    The recipient_hex is the NOSTR pubkey whose kind 9735 receipt will be
    published (typically the content author), NOT the LNURL provider.
    """
    event = (
        EventBuilder(Kind(9734), comment)
        .tags([
            Tag.public_key(PublicKey.parse(recipient_hex)),
            Tag.custom(TagKind.AMOUNT(), [str(amount_msats)]),
            Tag.custom(TagKind.RELAYS(), relays),
        ])
        .sign_with_keys(bot_keys)
    )
    return event.as_json()


async def request_invoice(
    params: LnurlPayParams,
    amount_msats: int,
    zap_request_json: str | None = None,
    comment: str = "",
    timeout: float = 15.0,
) -> tuple[str, bool]:
    """Hit the LNURL callback, return (bolt11, is_zap).

    The returned invoice is verified before being handed back (LUD-06
    requires the payer to check it): its amount MUST equal `amount_msats`,
    otherwise LnurlSecurityError is raised — a malicious server could
    otherwise hand us an invoice for an arbitrary amount.

    `is_zap` is True iff a NIP-57 zap request was attached AND the invoice's
    description_hash commits to it (which is what makes the eventual kind
    9735 receipt verifiable). If the server ignored the zap request's
    commitment, the payment still proceeds but is_zap is False.
    """
    separator = "&" if "?" in params.callback else "?"
    url = f"{params.callback}{separator}amount={amount_msats}"
    is_zap = False

    if zap_request_json:
        url += f"&nostr={urllib.parse.quote(zap_request_json)}"
        is_zap = True
    elif params.comment_allowed > 0 and comment:
        trimmed = comment[: params.comment_allowed]
        url += f"&comment={urllib.parse.quote(trimmed)}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") == "ERROR":
        raise ValueError(f"LNURL-pay callback error: {data.get('reason')}")

    bolt11 = data.get("pr")
    if not bolt11:
        raise ValueError("LNURL-pay callback did not return a payment request")

    invoice = _verify_invoice_amount(bolt11, amount_msats)

    if is_zap:
        expected_hash = hashlib.sha256(
            zap_request_json.encode("utf-8"),
        ).hexdigest()
        if invoice.description_hash != expected_hash:
            log.warning(
                "LNURL callback invoice does not commit to the zap request "
                "(description_hash mismatch); paying as plain LNURL — the "
                "recipient will not get a verifiable zap receipt",
            )
            is_zap = False

    return bolt11, is_zap


def _verify_invoice_amount(bolt11_str: str, amount_msats: int):
    """Decode `bolt11_str` and require its amount to equal `amount_msats`.

    Returns the decoded invoice. Raises LnurlSecurityError on an
    undecodable invoice or an amount mismatch (including amountless
    invoices, where the payer's wallet would decide the amount).
    """
    try:
        invoice = bolt11_lib.decode(bolt11_str)
    except Exception as e:
        raise LnurlSecurityError(
            "LNURL callback returned an undecodable bolt11 invoice",
        ) from e
    if invoice.amount_msat != amount_msats:
        raise LnurlSecurityError(
            f"LNURL callback returned an invoice for "
            f"{invoice.amount_msat} msats, but {amount_msats} msats was "
            f"requested; refusing to pay",
        )
    return invoice


# -- Invoice wallet protocol ---------------------------------------------------


class InvoiceWallet(Protocol):
    """A wallet that can pay a bolt11 invoice.

    Implementations must return (total_sats_paid, fee_sats). If the route's
    fee exceeds `max_fee_percent` of the invoice amount, implementations
    should refuse the payment (typically by surfacing an error from the
    underlying node/service).
    """

    async def pay(
        self, bolt11: str, max_fee_percent: float | None = None,
    ) -> tuple[int, int]:
        ...


class BtcPayWallet:
    """InvoiceWallet backed by a BTCPay Server lightning node.

    Calls /api/v1/stores/{store_id}/lightning/BTC/invoices/pay with the
    `BOLT11` and optional `maxFeePercent` body fields. The store's API key
    must have the "send bitcoin" permission.
    """

    def __init__(
        self,
        url: str,
        store_id: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._store_id = store_id
        self._api_key = api_key
        self._timeout = timeout

    async def pay(
        self, bolt11: str, max_fee_percent: float | None = None,
    ) -> tuple[int, int]:
        """Pay `bolt11` via BTCPay.

        Raises PaymentOutcomeUnknown when the result cannot be determined
        (timeout, dropped connection, 5xx, or BTCPay reporting the payment
        as pending) — in those cases the payment may still settle, so the
        caller must not retry with a fresh invoice. Other exceptions mean
        the payment definitively did not happen.
        """
        endpoint = (
            f"{self._url}/api/v1/stores/{self._store_id}"
            f"/lightning/BTC/invoices/pay"
        )
        body: dict = {"BOLT11": bolt11}
        if max_fee_percent is not None:
            body["maxFeePercent"] = max_fee_percent

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    endpoint,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"token {self._api_key}",
                    },
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # The request never reached BTCPay: definitively not paid.
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            # The request may have reached BTCPay; the HTLC could still be
            # in flight and settle minutes later.
            raise PaymentOutcomeUnknown(
                f"BTCPay pay request did not complete: {e!r}",
            ) from e

        if resp.status_code >= 500:
            raise PaymentOutcomeUnknown(
                f"BTCPay returned HTTP {resp.status_code}; "
                f"the payment may still settle",
            )
        resp.raise_for_status()
        data = resp.json()

        status = str(data.get("status", "")).lower()
        if status == "failed":
            raise ValueError("BTCPay reports the payment failed")
        if status == "pending":
            raise PaymentOutcomeUnknown(
                "BTCPay reports the payment as pending; it may still settle",
            )

        total_msats = data.get("totalAmount", 0)
        if isinstance(total_msats, str):
            total_msats = int(total_msats)
        fee_msats = data.get("feeAmount", 0)
        if isinstance(fee_msats, str):
            fee_msats = int(fee_msats)

        return math.ceil(total_msats / 1000), math.ceil(fee_msats / 1000)


# -- LnurlPayer ---------------------------------------------------------------


class LnurlPayer:
    """LNURL-pay client with NIP-57 zap support and fee-tier retries.

    Construction:
        payer = LnurlPayer(keys=bot.keys, wallet=BtcPayWallet(...))

    Use `payer.pay(lud16=..., amount_sats=...)` to send a payment. Pass
    `zap_target_pubkey=<hex>` to attach a NIP-57 zap request so the
    recipient gets a public zap receipt visible on Nostr.
    """

    def __init__(
        self,
        keys: Keys | None,
        wallet: InvoiceWallet,
        *,
        fee_policy: FeePolicy = DEFAULT_FEE_POLICY,
        zap_relays: list[str] | None = None,
        default_comment: str = "",
    ) -> None:
        """
        Args:
            keys: Bot's Keys, used to sign NIP-57 zap requests. May be None
                if you never pass `zap_target_pubkey` to pay().
            wallet: Anything implementing the InvoiceWallet protocol.
            fee_policy: Graduated fee-tier retry policy.
            zap_relays: Relays to advertise in zap requests. Defaults to
                DEFAULT_ZAP_RELAYS.
            default_comment: Used when caller doesn't pass `comment` and
                doesn't pass `source_url`.
        """
        self._keys = keys
        self._wallet = wallet
        self._fee_policy = fee_policy
        self._zap_relays = zap_relays if zap_relays is not None else list(DEFAULT_ZAP_RELAYS)
        self._default_comment = default_comment

    async def pay(
        self,
        lud16: str,
        amount_sats: int,
        *,
        zap_target_pubkey: str | None = None,
        comment: str | None = None,
        source_url: str = "",
    ) -> PayoutResult:
        """Pay `amount_sats` to `lud16` using graduated fee-tier retries.

        If `zap_target_pubkey` is set AND the recipient's LNURL provider
        advertises `allowsNostr`, a NIP-57 zap request is attached so the
        recipient receives a public zap receipt (the kind 9735 names
        zap_target_pubkey, which is typically the content author).

        `comment` overrides the default; `source_url` is embedded in the
        default comment (e.g., a tweet URL or nevent for context).

        Raises:
            LnurlSecurityError: the server returned an invoice whose amount
                doesn't match the request. Nothing was paid.
            PaymentOutcomeUnknown: a payment attempt's outcome could not be
                determined (timeout / pending). No further tier is tried —
                paying another invoice could double-pay. Reconcile with the
                wallet before retrying this payout.
            RuntimeError: every fee tier definitively failed. Nothing was
                paid.
        """
        params = await resolve_lud16(lud16)
        msg = self._build_comment(comment, source_url)
        tiers = self._fee_policy.tiers(amount_sats)

        for i, (tier_sats, max_fee_pct) in enumerate(tiers):
            if i > 0:
                log.info(
                    "LnurlPayer tier %d failed for %s, trying tier %d: "
                    "%d sats at %.1f%% fee cap",
                    i, lud16, i + 1, tier_sats, max_fee_pct,
                )
            result = await self._attempt_tier(
                params=params,
                amount_sats=tier_sats,
                max_fee_pct=max_fee_pct,
                lud16=lud16,
                zap_target_pubkey=zap_target_pubkey,
                comment=msg,
            )
            if result is not None:
                return result

        log.error("LnurlPayer: all fee tiers exhausted for %s", lud16)
        raise RuntimeError(
            f"Lightning payment to {lud16} failed at all fee tiers"
        )

    def _build_comment(self, comment: str | None, source_url: str) -> str:
        if comment is not None:
            return comment
        if source_url:
            if source_url.startswith(("nevent1", "note1", "naddr1")):
                source_link = f"https://njump.me/{source_url}"
            else:
                source_link = source_url
            base = self._default_comment or "Payment"
            return f"{base} for {source_link}"
        return self._default_comment

    async def _attempt_tier(
        self,
        params: LnurlPayParams,
        amount_sats: int,
        max_fee_pct: float,
        lud16: str,
        zap_target_pubkey: str | None,
        comment: str,
    ) -> PayoutResult | None:
        """Try a single (amount, fee_cap) tier.

        Returns None on definitive failure (caller tries the next tier).
        Raises LnurlSecurityError or PaymentOutcomeUnknown to abort all
        tiers.
        """
        amount_msats = amount_sats * 1000

        if amount_msats < params.min_sendable:
            log.warning(
                "%d sats below min %d for %s, skipping",
                amount_sats, params.min_sendable // 1000, lud16,
            )
            return PayoutResult(status="skipped")
        if amount_msats > params.max_sendable:
            log.warning(
                "%d sats above max %d for %s, skipping",
                amount_sats, params.max_sendable // 1000, lud16,
            )
            return PayoutResult(status="skipped")

        zap_json: str | None = None
        if params.allows_nostr and zap_target_pubkey and self._keys is not None:
            try:
                zap_json = create_zap_request(
                    recipient_hex=zap_target_pubkey,
                    amount_msats=amount_msats,
                    relays=self._zap_relays,
                    comment=comment,
                    bot_keys=self._keys,
                )
            except Exception:
                log.warning(
                    "Failed to create zap request for %s, falling back to plain LNURL",
                    lud16, exc_info=True,
                )

        try:
            bolt11, is_zap = await request_invoice(
                params, amount_msats,
                zap_request_json=zap_json, comment=comment,
            )
        except LnurlSecurityError:
            # Wrong-amount or undecodable invoice: the server is broken or
            # hostile. Another tier would get the same treatment; abort.
            log.error(
                "LNURL server for %s returned a bad invoice; aborting", lud16,
            )
            raise
        except Exception:
            log.info(
                "Invoice request failed at %.1f%% tier for %d sats to %s",
                max_fee_pct, amount_sats, lud16, exc_info=True,
            )
            return None

        try:
            total_sats, fee_sats = await self._wallet.pay(
                bolt11, max_fee_percent=max_fee_pct,
            )
        except PaymentOutcomeUnknown:
            # The payment may still settle. Trying another tier would pay a
            # SECOND invoice for the same payout — never retry past this.
            log.error(
                "Payment outcome unknown for %d sats to %s; aborting tier "
                "retries to avoid double-payment",
                amount_sats, lud16,
            )
            raise
        except Exception:
            log.info(
                "Payment failed at %.1f%% fee cap for %d sats to %s",
                max_fee_pct, amount_sats, lud16, exc_info=True,
            )
            return None

        log.info(
            "Paid %d sats to %s (fee %d sats, cap %.1f%%)%s",
            amount_sats, lud16, fee_sats, max_fee_pct,
            " (zap)" if is_zap else "",
        )
        return PayoutResult(
            status="paid",
            fee_sats=fee_sats,
            actual_sats=amount_sats,
        )
