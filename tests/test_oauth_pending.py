# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from weakref import ref

import pytest

from jasper.web import google_setup, oauth_pending, spotify_setup
from jasper.web.oauth_pending import PendingFlows, new_nonce


@pytest.mark.parametrize("payload", [
    ("alice", "verifier-abc-123", "challenge-xyz-987"),
    ("alice", "verifier-abc-123"),
    ("alice", None),
])
@pytest.mark.parametrize("age,valid", [(0, True), (600, True), (600.001, False)])
def test_pending_flow_expiry_and_single_use(monkeypatch, payload, age, valid):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(oauth_pending.time, "monotonic", clock)
    pending = PendingFlows()
    pending.add("nonce", payload)
    clock.return_value += age
    assert pending.consume("unknown") is None
    assert pending.consume("nonce") == (payload if valid else None)
    assert pending.consume("nonce") is None


def test_add_releases_expired_payloads_and_keeps_fresh_entries(monkeypatch):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(oauth_pending.time, "monotonic", clock)
    pending = PendingFlows()
    payload = Mock()
    old = ref(payload)
    pending.add("expired", payload)
    del payload
    clock.return_value = 701.0
    pending.add("fresh", ("a", "v1", "c1"))
    assert old() is None
    assert pending.consume("fresh") == ("a", "v1", "c1")
    assert pending.consume("expired") is None


def test_new_nonce_unique_and_url_safe():
    nonces = {new_nonce() for _ in range(100)}
    assert len(nonces) == 100
    for nonce in nonces:
        assert all(c.isalnum() or c in "-_" for c in nonce)
        assert len(nonce) >= 16


def test_provider_stores_are_isolated(monkeypatch):
    for wizard in (spotify_setup, google_setup):
        monkeypatch.setattr(wizard, "_PENDING_FLOWS", PendingFlows())
    spotify_setup._PENDING_FLOWS.add("spotify", ("a", "v", "c"))
    google_setup._PENDING_FLOWS.add("google", ("b", "w"))
    assert google_setup._PENDING_FLOWS.consume("spotify") is None
    assert spotify_setup._PENDING_FLOWS.consume("google") is None
    assert spotify_setup._PENDING_FLOWS.consume("spotify") == ("a", "v", "c")
    assert google_setup._PENDING_FLOWS.consume("google") == ("b", "w")


def test_concurrent_callbacks_consume_once():
    pending = PendingFlows()
    pending.add("nonce", ("alice", "verifier", "challenge"))
    barrier = Barrier(8)

    def consume(index):
        barrier.wait(timeout=5)
        pending.add(str(index), ("other", "v", "c"))
        return pending.consume("nonce")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(consume, range(8)))
    assert results.count(("alice", "verifier", "challenge")) == 1
    assert results.count(None) == 7
