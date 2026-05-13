"""Tests for nostrbot_sdk.lnurl_pay: LnurlPayer, fee tiers, BtcPayWallet."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nostr_sdk import Keys

from nostrbot_sdk.lnurl_pay import (
    DEFAULT_FEE_POLICY,
    DEFAULT_ZAP_RELAYS,
    BtcPayWallet,
    FeePolicy,
    LnurlPayParams,
    LnurlPayer,
    PayoutResult,
    create_zap_request,
    request_invoice,
    resolve_lud16,
)


# -- FeePolicy.tiers (port of creator_payout test suite) ----------------------


class TestFeePolicy:
    """Graduated fee-tier policy: full share when fee fits operator
    contribution, then reduce creator share as fee budget grows."""

    def test_21_sat_share_full_tier_table(self) -> None:
        policy = FeePolicy()  # defaults: contribution=2, budgets=(1,2,3,4,5,7,10)
        tiers = policy.tiers(21)
        expected = [
            (21, 1 / 21 * 100),
            (21, 2 / 21 * 100),
            (20, 3 / 20 * 100),
            (19, 4 / 19 * 100),
            (18, 5 / 18 * 100),
            (16, 7 / 16 * 100),
            (13, 10 / 13 * 100),
        ]
        assert len(tiers) == len(expected)
        for (got_sats, got_pct), (exp_sats, exp_pct) in zip(tiers, expected):
            assert got_sats == exp_sats
            assert got_pct == pytest.approx(exp_pct)

    def test_early_tiers_send_full_share(self) -> None:
        policy = FeePolicy()
        tiers = policy.tiers(50)
        for i, (creator_sats, _) in enumerate(tiers):
            fee_budget = policy.fee_budgets_sats[i]
            if fee_budget <= policy.operator_contribution_sats:
                assert creator_sats == 50

    def test_later_tiers_reduce_creator(self) -> None:
        policy = FeePolicy()
        tiers = policy.tiers(50)
        max_outflow = 50 + policy.operator_contribution_sats
        for i, (creator_sats, _) in enumerate(tiers):
            fee_budget = policy.fee_budgets_sats[i]
            if fee_budget > policy.operator_contribution_sats:
                assert creator_sats == max_outflow - fee_budget

    def test_fee_cap_matches_budget(self) -> None:
        policy = FeePolicy()
        for amount in (10, 21, 50, 100):
            tiers = policy.tiers(amount)
            for i, (creator_sats, fee_pct) in enumerate(tiers):
                max_fee_sats = creator_sats * fee_pct / 100
                assert max_fee_sats == pytest.approx(policy.fee_budgets_sats[i])

    def test_tiers_escalate_fee_cap(self) -> None:
        tiers = FeePolicy().tiers(21)
        for i in range(1, len(tiers)):
            assert tiers[i][1] > tiers[i - 1][1]

    def test_small_amount_skips_impossible_tiers(self) -> None:
        tiers = FeePolicy().tiers(5)
        for creator_sats, _ in tiers:
            assert creator_sats >= 1

    def test_large_amount_all_tiers_present(self) -> None:
        policy = FeePolicy()
        tiers = policy.tiers(100)
        assert len(tiers) == len(policy.fee_budgets_sats)

    def test_max_outflow_bounded(self) -> None:
        policy = FeePolicy()
        tiers = policy.tiers(21)
        max_outflow = 21 + policy.operator_contribution_sats
        for creator_sats, fee_pct in tiers:
            max_fee = creator_sats * fee_pct / 100
            assert creator_sats + max_fee <= max_outflow + 0.01

    def test_custom_policy_uses_custom_budgets(self) -> None:
        policy = FeePolicy(operator_contribution_sats=5, fee_budgets_sats=(1, 5, 20))
        tiers = policy.tiers(100)
        # Budgets 1 and 5 are <= contribution: full 100. Budget 20: 85.
        assert tiers[0] == (100, 1 / 100 * 100)
        assert tiers[1] == (100, 5 / 100 * 100)
        assert tiers[2] == (85, 20 / 85 * 100)

    def test_default_fee_policy_exported(self) -> None:
        assert DEFAULT_FEE_POLICY.operator_contribution_sats == 2
        assert DEFAULT_FEE_POLICY.fee_budgets_sats == (1, 2, 3, 4, 5, 7, 10)


# -- resolve_lud16 ------------------------------------------------------------


def _httpx_response(json_body: dict | None = None, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_body if json_body is not None else {}
    resp.raise_for_status = MagicMock()
    resp.status_code = status
    return resp


def _mock_async_client(response: MagicMock) -> MagicMock:
    """Build a mock httpx.AsyncClient context-manager returning `response`."""
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, client


async def test_resolve_lud16_happy_path() -> None:
    ctx, client = _mock_async_client(_httpx_response({
        "callback": "https://example.com/cb",
        "minSendable": 1000,
        "maxSendable": 10_000_000,
        "commentAllowed": 200,
        "allowsNostr": True,
        "nostrPubkey": "aa" * 32,
    }))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        params = await resolve_lud16("alice@unsaltedbutter.ai")
    assert params.callback == "https://example.com/cb"
    assert params.min_sendable == 1000
    assert params.max_sendable == 10_000_000
    assert params.comment_allowed == 200
    assert params.allows_nostr is True
    assert params.nostr_pubkey == "aa" * 32
    client.get.assert_awaited_once()
    called_url = client.get.call_args[0][0]
    assert called_url == "https://unsaltedbutter.ai/.well-known/lnurlp/alice"


async def test_resolve_lud16_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="Invalid lud16"):
        await resolve_lud16("not-an-address")
    with pytest.raises(ValueError, match="Invalid lud16"):
        await resolve_lud16("@unsaltedbutter.ai")
    with pytest.raises(ValueError, match="Invalid lud16"):
        await resolve_lud16("alice@")


async def test_resolve_lud16_rejects_error_response() -> None:
    ctx, _ = _mock_async_client(_httpx_response({"status": "ERROR", "reason": "nope"}))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        with pytest.raises(ValueError, match="LNURL-pay error"):
            await resolve_lud16("alice@unsaltedbutter.ai")


async def test_resolve_lud16_rejects_missing_fields() -> None:
    ctx, _ = _mock_async_client(_httpx_response({"callback": "x"}))  # no minSendable
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        with pytest.raises(ValueError, match="Invalid LNURL-pay response"):
            await resolve_lud16("alice@unsaltedbutter.ai")


async def test_resolve_lud16_default_allows_nostr_false() -> None:
    ctx, _ = _mock_async_client(_httpx_response({
        "callback": "https://x.com/cb",
        "minSendable": 1000,
        "maxSendable": 1000,
    }))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        params = await resolve_lud16("alice@unsaltedbutter.ai")
    assert params.allows_nostr is False
    assert params.nostr_pubkey is None
    assert params.comment_allowed == 0


# -- create_zap_request --------------------------------------------------------


def test_create_zap_request_signs_event_with_correct_tags() -> None:
    keys = Keys.generate()
    recipient = Keys.generate().public_key().to_hex()
    json_str = create_zap_request(
        recipient_hex=recipient,
        amount_msats=3_000_000,
        relays=["wss://nos.lol", "wss://relay.damus.io"],
        comment="hi",
        bot_keys=keys,
    )
    obj = json.loads(json_str)
    assert obj["kind"] == 9734
    assert obj["pubkey"] == keys.public_key().to_hex()
    assert obj["content"] == "hi"
    tags = {t[0]: t for t in obj["tags"]}
    assert tags["p"][1] == recipient
    assert tags["amount"][1] == "3000000"
    assert tags["relays"][1:] == ["wss://nos.lol", "wss://relay.damus.io"]
    # Signed event has a valid signature field.
    assert "sig" in obj and len(obj["sig"]) == 128


# -- request_invoice ----------------------------------------------------------


def _params(callback: str = "https://x.com/cb", **kw) -> LnurlPayParams:
    return LnurlPayParams(
        callback=callback,
        min_sendable=kw.get("min_sendable", 1000),
        max_sendable=kw.get("max_sendable", 10_000_000),
        comment_allowed=kw.get("comment_allowed", 0),
        allows_nostr=kw.get("allows_nostr", False),
        nostr_pubkey=kw.get("nostr_pubkey", None),
    )


async def test_request_invoice_plain() -> None:
    ctx, client = _mock_async_client(_httpx_response({"pr": "lnbc1..."}))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        bolt11, is_zap = await request_invoice(_params(), amount_msats=1000)
    assert bolt11 == "lnbc1..."
    assert is_zap is False
    url = client.get.call_args[0][0]
    assert "amount=1000" in url


async def test_request_invoice_with_zap_request() -> None:
    ctx, client = _mock_async_client(_httpx_response({"pr": "lnbc1..."}))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        bolt11, is_zap = await request_invoice(
            _params(), amount_msats=1000,
            zap_request_json='{"kind":9734}',
        )
    assert is_zap is True
    url = client.get.call_args[0][0]
    assert "nostr=" in url
    assert "%22kind%22%3A9734" in url


async def test_request_invoice_with_comment_when_allowed() -> None:
    ctx, client = _mock_async_client(_httpx_response({"pr": "lnbc1..."}))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        await request_invoice(
            _params(comment_allowed=100), amount_msats=1000, comment="hello world",
        )
    url = client.get.call_args[0][0]
    assert "comment=hello%20world" in url


async def test_request_invoice_truncates_comment_to_limit() -> None:
    ctx, client = _mock_async_client(_httpx_response({"pr": "lnbc1..."}))
    long_comment = "a" * 500
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        await request_invoice(
            _params(comment_allowed=10), amount_msats=1000, comment=long_comment,
        )
    url = client.get.call_args[0][0]
    assert "comment=aaaaaaaaaa&" in url or url.endswith("comment=aaaaaaaaaa")


async def test_request_invoice_skips_comment_when_not_allowed() -> None:
    ctx, client = _mock_async_client(_httpx_response({"pr": "lnbc1..."}))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        await request_invoice(
            _params(comment_allowed=0), amount_msats=1000, comment="hi",
        )
    url = client.get.call_args[0][0]
    assert "comment=" not in url


async def test_request_invoice_rejects_error_response() -> None:
    ctx, _ = _mock_async_client(_httpx_response({"status": "ERROR", "reason": "bad"}))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        with pytest.raises(ValueError, match="LNURL-pay callback error"):
            await request_invoice(_params(), amount_msats=1000)


async def test_request_invoice_rejects_missing_pr() -> None:
    ctx, _ = _mock_async_client(_httpx_response({}))
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        with pytest.raises(ValueError, match="did not return a payment request"):
            await request_invoice(_params(), amount_msats=1000)


# -- BtcPayWallet -------------------------------------------------------------


async def test_btcpay_wallet_returns_total_and_fee_in_sats() -> None:
    ctx, client = _mock_async_client(_httpx_response({
        "totalAmount": 1003000,  # 1003 sats in msats
        "feeAmount": 3000,       # 3 sats in msats
    }))
    wallet = BtcPayWallet(
        url="https://btcpay.example.com",
        store_id="store123",
        api_key="key123",
    )
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        total, fee = await wallet.pay("lnbc1...", max_fee_percent=1.0)
    assert total == 1003
    assert fee == 3
    body = client.post.call_args[1]["json"]
    assert body["BOLT11"] == "lnbc1..."
    assert body["maxFeePercent"] == 1.0
    headers = client.post.call_args[1]["headers"]
    assert headers["Authorization"] == "token key123"
    posted_url = client.post.call_args[0][0]
    assert "/api/v1/stores/store123/lightning/BTC/invoices/pay" in posted_url


async def test_btcpay_wallet_handles_string_amounts() -> None:
    """BTCPay sometimes returns totalAmount/feeAmount as strings."""
    ctx, _ = _mock_async_client(_httpx_response({
        "totalAmount": "500500",  # 500.5 sats -> rounds to 501
        "feeAmount": "500",       # 0.5 sats -> rounds to 1
    }))
    wallet = BtcPayWallet("https://btcpay.example.com", "s", "k")
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        total, fee = await wallet.pay("lnbc1...")
    assert total == 501
    assert fee == 1


async def test_btcpay_wallet_omits_max_fee_percent_when_none() -> None:
    ctx, client = _mock_async_client(_httpx_response({"totalAmount": 0, "feeAmount": 0}))
    wallet = BtcPayWallet("https://btcpay.example.com", "s", "k")
    with patch("nostrbot_sdk.lnurl_pay.httpx.AsyncClient", return_value=ctx):
        await wallet.pay("lnbc1...")
    body = client.post.call_args[1]["json"]
    assert "maxFeePercent" not in body


def test_btcpay_wallet_strips_trailing_slash_from_url() -> None:
    wallet = BtcPayWallet("https://btcpay.example.com/", "s", "k")
    assert wallet._url == "https://btcpay.example.com"


# -- LnurlPayer end-to-end ----------------------------------------------------


class _FakeWallet:
    """Test InvoiceWallet that records calls and returns canned responses."""

    def __init__(self, total: int = 500, fee: int = 1, *, fail_below_pct: float | None = None):
        self.total = total
        self.fee = fee
        self.fail_below_pct = fail_below_pct
        self.calls: list[tuple[str, float | None]] = []

    async def pay(self, bolt11: str, max_fee_percent: float | None = None) -> tuple[int, int]:
        self.calls.append((bolt11, max_fee_percent))
        if (
            self.fail_below_pct is not None
            and max_fee_percent is not None
            and max_fee_percent < self.fail_below_pct
        ):
            raise RuntimeError("route too expensive")
        return self.total, self.fee


async def test_payer_pay_happy_path_with_zap() -> None:
    keys = Keys.generate()
    wallet = _FakeWallet(total=500, fee=1)
    payer = LnurlPayer(keys=keys, wallet=wallet)

    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params(allows_nostr=True, comment_allowed=200)),
        ),
        patch(
            "nostrbot_sdk.lnurl_pay.request_invoice",
            new=AsyncMock(return_value=("lnbc500n1fake", True)),
        ) as mock_request,
    ):
        target = Keys.generate().public_key().to_hex()
        result = await payer.pay(
            lud16="alice@unsaltedbutter.ai",
            amount_sats=500,
            zap_target_pubkey=target,
            comment="thanks",
        )

    assert result == PayoutResult(status="paid", fee_sats=1, actual_sats=500)
    # First successful tier wins; only one wallet.pay call.
    assert len(wallet.calls) == 1
    # request_invoice was given a zap request (allows_nostr + zap_target_pubkey).
    request_kwargs = mock_request.call_args[1]
    assert request_kwargs["zap_request_json"] is not None
    zap_obj = json.loads(request_kwargs["zap_request_json"])
    assert zap_obj["kind"] == 9734
    assert any(t[0] == "p" and t[1] == target for t in zap_obj["tags"])


async def test_payer_pay_without_zap_target_uses_plain_lnurl() -> None:
    wallet = _FakeWallet()
    payer = LnurlPayer(keys=Keys.generate(), wallet=wallet)
    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params(allows_nostr=True)),
        ),
        patch(
            "nostrbot_sdk.lnurl_pay.request_invoice",
            new=AsyncMock(return_value=("lnbc500n1fake", False)),
        ) as mock_request,
    ):
        result = await payer.pay(lud16="alice@unsaltedbutter.ai", amount_sats=500)
    assert result.status == "paid"
    assert mock_request.call_args[1]["zap_request_json"] is None


async def test_payer_pay_escalates_through_fee_tiers() -> None:
    """First tier (1 sat fee = 0.2% cap on 500 sat invoice) fails; second tier passes."""
    # tier 1: amount 500, fee_pct = 0.2%. fail_below_pct=0.3 => fails.
    # tier 2: amount 500, fee_pct = 0.4%. fail_below_pct=0.3 => succeeds.
    wallet = _FakeWallet(fail_below_pct=0.3)
    payer = LnurlPayer(keys=Keys.generate(), wallet=wallet)
    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params()),
        ),
        patch(
            "nostrbot_sdk.lnurl_pay.request_invoice",
            new=AsyncMock(return_value=("lnbc500n1fake", False)),
        ),
    ):
        result = await payer.pay(lud16="alice@unsaltedbutter.ai", amount_sats=500)
    assert result.status == "paid"
    assert len(wallet.calls) == 2
    # First call had a smaller fee cap than the second.
    assert wallet.calls[0][1] < wallet.calls[1][1]


async def test_payer_pay_below_min_returns_skipped() -> None:
    wallet = _FakeWallet()
    payer = LnurlPayer(keys=Keys.generate(), wallet=wallet)
    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params(min_sendable=100_000_000)),
        ),
    ):
        result = await payer.pay(lud16="alice@unsaltedbutter.ai", amount_sats=10)
    assert result.status == "skipped"
    assert wallet.calls == []


async def test_payer_pay_above_max_returns_skipped() -> None:
    wallet = _FakeWallet()
    payer = LnurlPayer(keys=Keys.generate(), wallet=wallet)
    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params(max_sendable=100)),
        ),
    ):
        result = await payer.pay(lud16="alice@unsaltedbutter.ai", amount_sats=10)
    assert result.status == "skipped"


async def test_payer_pay_raises_when_all_tiers_fail() -> None:
    wallet = _FakeWallet(fail_below_pct=100)  # fail every tier
    payer = LnurlPayer(keys=Keys.generate(), wallet=wallet)
    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params()),
        ),
        patch(
            "nostrbot_sdk.lnurl_pay.request_invoice",
            new=AsyncMock(return_value=("lnbc500n1fake", False)),
        ),
    ):
        with pytest.raises(RuntimeError, match="failed at all fee tiers"):
            await payer.pay(lud16="alice@unsaltedbutter.ai", amount_sats=500)


async def test_payer_pay_without_keys_falls_back_when_zap_target_set() -> None:
    """If keys=None but zap_target_pubkey is passed, do plain LNURL (no zap)."""
    wallet = _FakeWallet()
    payer = LnurlPayer(keys=None, wallet=wallet)
    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params(allows_nostr=True)),
        ),
        patch(
            "nostrbot_sdk.lnurl_pay.request_invoice",
            new=AsyncMock(return_value=("lnbc500n1fake", False)),
        ) as mock_request,
    ):
        await payer.pay(
            lud16="alice@unsaltedbutter.ai",
            amount_sats=500,
            zap_target_pubkey="aa" * 32,
        )
    assert mock_request.call_args[1]["zap_request_json"] is None


async def test_payer_pay_recipient_disallows_nostr_skips_zap() -> None:
    """allows_nostr=False on the LNURL params -> no zap request."""
    wallet = _FakeWallet()
    payer = LnurlPayer(keys=Keys.generate(), wallet=wallet)
    with (
        patch(
            "nostrbot_sdk.lnurl_pay.resolve_lud16",
            new=AsyncMock(return_value=_params(allows_nostr=False)),
        ),
        patch(
            "nostrbot_sdk.lnurl_pay.request_invoice",
            new=AsyncMock(return_value=("lnbc500n1fake", False)),
        ) as mock_request,
    ):
        await payer.pay(
            lud16="alice@unsaltedbutter.ai",
            amount_sats=500,
            zap_target_pubkey="aa" * 32,
        )
    assert mock_request.call_args[1]["zap_request_json"] is None


# -- _build_comment behavior ---------------------------------------------------


def test_build_comment_uses_explicit_comment_when_given() -> None:
    payer = LnurlPayer(keys=None, wallet=_FakeWallet(), default_comment="default")
    assert payer._build_comment("explicit", "https://x.com/y") == "explicit"
    assert payer._build_comment("", "https://x.com/y") == ""  # empty string is explicit


def test_build_comment_default_when_no_args() -> None:
    payer = LnurlPayer(keys=None, wallet=_FakeWallet(), default_comment="From MyBot")
    assert payer._build_comment(None, "") == "From MyBot"


def test_build_comment_appends_source_url_to_default() -> None:
    payer = LnurlPayer(keys=None, wallet=_FakeWallet(), default_comment="From MyBot")
    msg = payer._build_comment(None, "https://example.com/post/1")
    assert msg == "From MyBot for https://example.com/post/1"


def test_build_comment_wraps_nostr_source_in_njump_url() -> None:
    payer = LnurlPayer(keys=None, wallet=_FakeWallet(), default_comment="MyBot")
    msg = payer._build_comment(None, "nevent1xxxyyyzzz")
    assert msg == "MyBot for https://njump.me/nevent1xxxyyyzzz"


def test_build_comment_falls_back_to_payment_when_no_default() -> None:
    payer = LnurlPayer(keys=None, wallet=_FakeWallet(), default_comment="")
    msg = payer._build_comment(None, "https://x.com/y")
    assert msg == "Payment for https://x.com/y"


# -- Constants -----------------------------------------------------------------


def test_default_zap_relays_are_real() -> None:
    assert "wss://nos.lol" in DEFAULT_ZAP_RELAYS
    assert all(r.startswith("wss://") for r in DEFAULT_ZAP_RELAYS)
