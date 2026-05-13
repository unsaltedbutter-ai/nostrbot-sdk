"""Pure NIP-57 zap receipt validation (no I/O, no side effects).

Validates a kind 9735 zap receipt per NIP-57 checks 1-6:
  1. 9735 author must be the configured LNURL provider's nostr pubkey
  2. bolt11 description_hash must equal SHA-256 of the description tag
  3. Embedded 9734 zap request must have a valid signature
  4. 9734 must be kind 9734
  5. 9734 'p' tag must reference the bot's pubkey (zap was for us)
  6. If 9734 has 'amount' tag, it must match the bolt11 amount
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import bolt11 as bolt11_lib
from nostr_sdk import Event as NostrEvent

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidatedZap:
    """Result of a successful NIP-57 validation."""

    event_id: str       # 9735 event ID
    sender_hex: str     # zapper's pubkey (from 9734 author)
    amount_sats: int    # from bolt11
    bolt11: str         # raw bolt11 string


def validate_zap_receipt(
    event: NostrEvent,
    bot_pubkey_hex: str,
    zap_provider_pubkey_hex: str,
) -> ValidatedZap | None:
    """Validate a kind 9735 zap receipt per NIP-57.

    Returns ValidatedZap if all 6 checks pass, None otherwise.
    Pure validation only: no network calls, no DMs, no database access.
    """
    event_id: str = event.id().to_hex()

    # -- CHECK 1: 9735 must be signed by our LNURL provider --
    receipt_author: str = event.author().to_hex()
    if receipt_author != zap_provider_pubkey_hex:
        log.warning(
            "Zap receipt %s: author %s does not match provider %s, rejected",
            event_id, receipt_author[:16], zap_provider_pubkey_hex[:16],
        )
        return None

    # -- Extract tags --
    bolt11_str: str | None = None
    description_json: str | None = None
    for tag in event.tags().to_vec():
        tag_vec = tag.as_vec()
        if len(tag_vec) >= 2:
            if tag_vec[0] == "bolt11":
                bolt11_str = tag_vec[1]
            elif tag_vec[0] == "description":
                description_json = tag_vec[1]

    if not bolt11_str or not description_json:
        log.warning("Zap receipt %s missing bolt11 or description tag", event_id)
        return None

    # -- Decode bolt11 invoice --
    try:
        invoice = bolt11_lib.decode(bolt11_str)
    except Exception:
        log.warning("Zap receipt %s: failed to decode bolt11", event_id)
        return None

    amount_msats: int | None = invoice.amount_msat
    if not amount_msats or amount_msats <= 0:
        log.warning("Zap receipt %s: zero or missing amount", event_id)
        return None
    amount_sats: int = amount_msats // 1000

    # -- CHECK 2: bolt11 description_hash must match SHA-256 of description tag --
    expected_hash: str = hashlib.sha256(description_json.encode("utf-8")).hexdigest()
    if not invoice.description_hash:
        log.warning(
            "Zap receipt %s: bolt11 missing description_hash (not a valid zap invoice)",
            event_id,
        )
        return None
    if invoice.description_hash != expected_hash:
        log.warning("Zap receipt %s: bolt11 description_hash mismatch", event_id)
        return None

    # -- CHECK 3: Parse 9734 and verify its signature --
    try:
        zap_request_event = NostrEvent.from_json(description_json)
    except Exception:
        log.warning(
            "Zap receipt %s: embedded 9734 is not a valid nostr event", event_id,
        )
        return None

    if not zap_request_event.verify():
        log.warning(
            "Zap receipt %s: embedded 9734 failed signature verification", event_id,
        )
        return None

    # -- CHECK 4: Must be kind 9734 --
    if zap_request_event.kind().as_u16() != 9734:
        log.warning(
            "Zap receipt %s: embedded event is kind %d, expected 9734",
            event_id, zap_request_event.kind().as_u16(),
        )
        return None

    # -- CHECK 5: 9734 'p' tag must reference our bot pubkey --
    p_tags: list[str] = []
    for tag in zap_request_event.tags().to_vec():
        tag_vec = tag.as_vec()
        if len(tag_vec) >= 2 and tag_vec[0] == "p":
            p_tags.append(tag_vec[1])

    if bot_pubkey_hex not in p_tags:
        log.warning(
            "Zap receipt %s: 9734 p-tags %s do not include bot pubkey %s, rejected",
            event_id, p_tags, bot_pubkey_hex[:16],
        )
        return None

    # -- CHECK 6: If 9734 has 'amount' tag, it must match bolt11 --
    for tag in zap_request_event.tags().to_vec():
        tag_vec = tag.as_vec()
        if len(tag_vec) >= 2 and tag_vec[0] == "amount":
            try:
                requested_msats: int = int(tag_vec[1])
                if requested_msats != amount_msats:
                    log.warning(
                        "Zap receipt %s: amount mismatch, 9734 says %d msats, bolt11 says %d msats",
                        event_id, requested_msats, amount_msats,
                    )
                    return None
            except ValueError:
                log.warning(
                    "Zap receipt %s: malformed amount tag in 9734", event_id,
                )
                return None
            break

    # -- All checks passed --
    sender_hex: str = zap_request_event.author().to_hex()

    return ValidatedZap(
        event_id=event_id,
        sender_hex=sender_hex,
        amount_sats=amount_sats,
        bolt11=bolt11_str,
    )
