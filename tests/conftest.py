"""Shared test fixtures for nostrbot-sdk."""

from __future__ import annotations

import pytest

from nostr_sdk import nostr_sdk as _ns


@pytest.fixture(autouse=True)
def _isolate_uniffi_event_loop():
    """Save/restore nostr-sdk's global event loop around each test.

    NostrBot.start() records the running loop via uniffi_set_event_loop() so
    NIP-42 AUTH challenges arriving on Rust relay threads can dispatch the
    async authenticator. That global is process-wide, while pytest-asyncio
    runs each test on a fresh loop — without this reset, the first test that
    starts a bot would leave every later test's nostr-sdk calls pointed at a
    dead loop ("Future attached to a different loop").
    """
    saved = _ns._UNIFFI_GLOBAL_EVENT_LOOP
    yield
    _ns._UNIFFI_GLOBAL_EVENT_LOOP = saved
