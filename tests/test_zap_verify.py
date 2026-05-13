"""Tests for nostrbot_sdk.zap_verify: pure NIP-57 zap receipt validation."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

from nostrbot_sdk.zap_verify import ValidatedZap, validate_zap_receipt

# -- Fake hex pubkeys (64 char hex strings) ------------------------------------

BOT_PK: str = "aa" * 32
PROVIDER_PK: str = "bb" * 32
SENDER_PK: str = "cc" * 32
ATTACKER_PK: str = "dd" * 32

AMOUNT_MSATS: int = 3_000_000  # 3000 sats


# -- Mock builders -------------------------------------------------------------


def _make_tag(key: str, value: str) -> MagicMock:
    tag = MagicMock()
    tag.as_vec.return_value = [key, value]
    return tag


def _make_tags_obj(tags: list[MagicMock]) -> MagicMock:
    obj = MagicMock()
    obj.to_vec.return_value = tags
    return obj


def _make_9734_json(
    sender_pk: str = SENDER_PK,
    p_hex: str | None = BOT_PK,
    amount_msats: int | None = AMOUNT_MSATS,
    kind_num: int = 9734,
) -> str:
    tags: list[list[str]] = []
    if p_hex is not None:
        tags.append(["p", p_hex])
    if amount_msats is not None:
        tags.append(["amount", str(amount_msats)])
    tags.append(["relays", "wss://relay.damus.io"])

    return json.dumps({
        "id": "ee" * 32,
        "pubkey": sender_pk,
        "created_at": 1700000000,
        "kind": kind_num,
        "tags": tags,
        "content": "",
        "sig": "ff" * 32,
    })


def _make_9734_event(
    desc_json: str,
    verify_result: bool = True,
) -> MagicMock:
    obj = json.loads(desc_json)
    event = MagicMock()
    event.verify.return_value = verify_result

    kind_mock = MagicMock()
    kind_mock.as_u16.return_value = obj["kind"]
    event.kind.return_value = kind_mock

    author_mock = MagicMock()
    author_mock.to_hex.return_value = obj["pubkey"]
    event.author.return_value = author_mock

    mock_tags: list[MagicMock] = []
    for t in obj["tags"]:
        tag = MagicMock()
        tag.as_vec.return_value = t
        mock_tags.append(tag)
    event.tags.return_value = _make_tags_obj(mock_tags)

    return event


def _make_9735(
    author_pk: str = PROVIDER_PK,
    bolt11_str: str = "lnbc3000n1fake",
    description_json: str | None = None,
    event_id: str = "ab" * 32,
    include_bolt11: bool = True,
    include_description: bool = True,
) -> MagicMock:
    event = MagicMock()

    id_mock = MagicMock()
    id_mock.to_hex.return_value = event_id
    event.id.return_value = id_mock

    author_mock = MagicMock()
    author_mock.to_hex.return_value = author_pk
    event.author.return_value = author_mock

    tags: list[MagicMock] = []
    tags.append(_make_tag("p", BOT_PK))
    if include_bolt11:
        tags.append(_make_tag("bolt11", bolt11_str))
    if include_description and description_json is not None:
        tags.append(_make_tag("description", description_json))
    event.tags.return_value = _make_tags_obj(tags)

    return event


def _bolt11_mock(
    amount_msats: int = AMOUNT_MSATS,
    description_hash: str = "will_be_set",
) -> MagicMock:
    mock = MagicMock()
    mock.amount_msat = amount_msats
    mock.description_hash = description_hash
    return mock


def _valid_set() -> tuple[MagicMock, str, MagicMock, MagicMock]:
    """Return (receipt_event, desc_json, bolt11_mock, zap_request_event)."""
    desc_json = _make_9734_json()
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json)
    invoice = _bolt11_mock(description_hash=desc_hash)
    zap_req_event = _make_9734_event(desc_json, verify_result=True)
    return receipt, desc_json, invoice, zap_req_event


def _validate(receipt: MagicMock, invoice: MagicMock, zap_req: MagicMock) -> ValidatedZap | None:
    """Run validate_zap_receipt with patched bolt11 decode and NostrEvent.from_json."""
    with (
        patch("nostrbot_sdk.zap_verify.bolt11_lib.decode", return_value=invoice),
        patch("nostrbot_sdk.zap_verify.NostrEvent.from_json", return_value=zap_req),
    ):
        return validate_zap_receipt(receipt, BOT_PK, PROVIDER_PK)


# -- CHECK 1: 9735 author must be LNURL provider ------------------------------


def test_reject_wrong_author() -> None:
    receipt, _, invoice, zap_req = _valid_set()
    receipt.author.return_value.to_hex.return_value = ATTACKER_PK
    assert _validate(receipt, invoice, zap_req) is None


# -- Missing bolt11 tag -------------------------------------------------------


def test_reject_missing_bolt11() -> None:
    desc_json = _make_9734_json()
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json, include_bolt11=False)
    invoice = _bolt11_mock(description_hash=desc_hash)
    zap_req = _make_9734_event(desc_json)
    assert _validate(receipt, invoice, zap_req) is None


# -- Missing description tag ---------------------------------------------------


def test_reject_missing_description() -> None:
    receipt = _make_9735(include_description=False, description_json=None)
    invoice = _bolt11_mock()
    zap_req = MagicMock()
    assert _validate(receipt, invoice, zap_req) is None


# -- Bad bolt11 string ---------------------------------------------------------


def test_reject_bad_bolt11() -> None:
    desc_json = _make_9734_json()
    receipt = _make_9735(description_json=desc_json)
    zap_req = _make_9734_event(desc_json)

    with (
        patch("nostrbot_sdk.zap_verify.bolt11_lib.decode", side_effect=ValueError("invalid")),
        patch("nostrbot_sdk.zap_verify.NostrEvent.from_json", return_value=zap_req),
    ):
        result = validate_zap_receipt(receipt, BOT_PK, PROVIDER_PK)
    assert result is None


# -- Zero amount ---------------------------------------------------------------


def test_reject_zero_amount() -> None:
    desc_json = _make_9734_json()
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json)
    invoice = _bolt11_mock(amount_msats=0, description_hash=desc_hash)
    zap_req = _make_9734_event(desc_json)
    assert _validate(receipt, invoice, zap_req) is None


# -- CHECK 2: description_hash mismatch ---------------------------------------


def test_reject_description_hash_mismatch() -> None:
    receipt, _, _, zap_req = _valid_set()
    invoice = _bolt11_mock(description_hash="00" * 32)
    assert _validate(receipt, invoice, zap_req) is None


# -- CHECK 3: Invalid 9734 (not valid JSON event) -----------------------------


def test_reject_invalid_9734() -> None:
    garbage = '{"foo":"bar"}'
    desc_hash = hashlib.sha256(garbage.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=garbage)
    invoice = _bolt11_mock(description_hash=desc_hash)

    with (
        patch("nostrbot_sdk.zap_verify.bolt11_lib.decode", return_value=invoice),
        patch("nostrbot_sdk.zap_verify.NostrEvent.from_json", side_effect=Exception("invalid event")),
    ):
        result = validate_zap_receipt(receipt, BOT_PK, PROVIDER_PK)
    assert result is None


# -- CHECK 3b: Bad 9734 signature ---------------------------------------------


def test_reject_bad_9734_signature() -> None:
    desc_json = _make_9734_json()
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json)
    invoice = _bolt11_mock(description_hash=desc_hash)
    zap_req = _make_9734_event(desc_json, verify_result=False)
    assert _validate(receipt, invoice, zap_req) is None


# -- CHECK 4: Wrong kind in 9734 ----------------------------------------------


def test_reject_wrong_kind_9734() -> None:
    desc_json = _make_9734_json(kind_num=1)
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json)
    invoice = _bolt11_mock(description_hash=desc_hash)
    zap_req = _make_9734_event(desc_json)
    assert _validate(receipt, invoice, zap_req) is None


# -- CHECK 5: Wrong p-tag -----------------------------------------------------


def test_reject_wrong_p_tag() -> None:
    desc_json = _make_9734_json(p_hex=ATTACKER_PK)
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json)
    invoice = _bolt11_mock(description_hash=desc_hash)
    zap_req = _make_9734_event(desc_json)
    assert _validate(receipt, invoice, zap_req) is None


# -- CHECK 6: Amount mismatch -------------------------------------------------


def test_reject_amount_mismatch() -> None:
    desc_json = _make_9734_json(amount_msats=3_000_000)
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json)
    invoice = _bolt11_mock(amount_msats=1_000_000, description_hash=desc_hash)
    zap_req = _make_9734_event(desc_json)
    assert _validate(receipt, invoice, zap_req) is None


# -- Happy path: all checks pass -----------------------------------------------


def test_valid_zap_returns_validated_zap() -> None:
    receipt, desc_json, invoice, zap_req = _valid_set()
    result = _validate(receipt, invoice, zap_req)
    assert result is not None
    assert isinstance(result, ValidatedZap)
    assert result.event_id == "ab" * 32
    assert result.sender_hex == SENDER_PK
    assert result.amount_sats == 3000
    assert result.bolt11 == "lnbc3000n1fake"


# -- No amount tag in 9734 (optional, should still pass) ----------------------


def test_valid_zap_without_amount_tag() -> None:
    desc_json = _make_9734_json(amount_msats=None)
    desc_hash = hashlib.sha256(desc_json.encode("utf-8")).hexdigest()
    receipt = _make_9735(description_json=desc_json)
    invoice = _bolt11_mock(description_hash=desc_hash)
    zap_req = _make_9734_event(desc_json)
    result = _validate(receipt, invoice, zap_req)
    assert result is not None
    assert result.amount_sats == 3000
