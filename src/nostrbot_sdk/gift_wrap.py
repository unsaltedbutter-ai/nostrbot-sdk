"""NIP-59 gift wrap construction with optional timestamp control.

The stock ``nip59_make_gift_wrap_async`` randomizes the wrap's ``created_at``
up to two days into the past — which scrambles the ordering of multi-chunk
messages in recipient clients. :func:`make_gift_wrap_async` delegates to it by
default, but lets the caller pin the wrap's ``created_at``: the proxy assigns
each chunk a random backdate sorted in descending order, so clients show the
chunks in reading order while every wrap still carries an individually random
NIP-59-looking timestamp to relays.
"""

from __future__ import annotations

import logging
from typing import Optional, Union

from nostr_sdk import (
    Event,
    EventBuilder,
    Keys,
    Kind,
    PublicKey,
    Tag,
    Timestamp,
    nip44_encrypt,
    nip59_make_gift_wrap_async,
    nip59_make_seal_async,
)

logger = logging.getLogger(__name__)


async def make_gift_wrap_async(
    signer: Keys,
    receiver_pubkey: PublicKey,
    rumor: Event,
    *,
    expiration: Optional[object] = None,
    extra_tags: Optional[list[Tag]] = None,
    wrap_created_at: Optional[Union[int, Timestamp]] = None,
) -> Event:
    """Build a NIP-59 gift wrap around ``rumor`` for ``receiver_pubkey``.

    With ``wrap_created_at`` unset this delegates to the SDK's stock NIP-59
    helper (randomized timestamps). With it set, the seal is built the normal
    way and the wrap is assembled manually so its ``created_at`` is exactly
    ``wrap_created_at`` — callers can keep multi-chunk messages ordered by
    assigning descending timestamps while every wrap still carries an
    individually random-looking timestamp to relays.

    ``expiration`` is forwarded to the stock helper; in the manual path the
    caller passes its NIP-40 tag via ``extra_tags`` (the proxy stamps the same
    tag inside the rumor and on the wrap).
    """
    if wrap_created_at is None:
        return await nip59_make_gift_wrap_async(
            signer,
            receiver_pubkey,
            rumor,
            expiration=expiration,
            extra_tags=extra_tags,
        )
    seal = await nip59_make_seal_async(signer, receiver_pubkey, rumor)
    ephemeral = Keys.generate()
    wrap_content = ephemeral.nip44_encrypt(receiver_pubkey, seal.as_json())
    tags = [Tag.public_key(receiver_pubkey)]
    if extra_tags:
        tags.extend(extra_tags)
    created_at = (
        wrap_created_at
        if isinstance(wrap_created_at, Timestamp)
        else Timestamp.from_secs(int(wrap_created_at))
    )
    return await (
        EventBuilder(Kind(1059), wrap_content)
        .custom_created_at(created_at)
        .tags(tags)
        .finalize_async(ephemeral)
    )
