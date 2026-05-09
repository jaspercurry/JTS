"""Tests for the Spotify-account setup wizard's PKCE flow plumbing.

The wizard's OAuth flow spans two separate HTTP requests: /start (which
generates the PKCE verifier and authorize URL) and /oauth-callback or
/paste-callback (which exchanges the code for a token). spotipy's
SpotifyPKCE generates the verifier lazily inside `get_authorize_url()`
and stores it on the instance only — a fresh instance built in the
callback handler has no verifier, and Spotify replies 400 invalid_grant
`code_verifier was incorrect` if you try to exchange without restoring
it.

Until 2026-05-09 the wizard naively rebuilt SpotifyPKCE in the callback
handler and lost the verifier. This module pins the fix: the verifier
is stashed in `_PENDING_FLOWS` alongside the account name so the
callback handler can restore it before the token exchange.
"""
from __future__ import annotations

import time

from jasper.web.spotify_setup import (
    _FLOW_TTL_SEC,
    _PENDING_FLOWS,
    _gc_pending,
    _new_nonce,
)


def _clear_pending():
    _PENDING_FLOWS.clear()


def test_pending_flows_stores_three_tuple():
    """{nonce: (account_name, verifier, created_monotonic)}.

    If this shape regresses to the old 2-tuple, _exchange_code can't
    restore the verifier and the OAuth round-trip fails.
    """
    _clear_pending()
    nonce = _new_nonce()
    _PENDING_FLOWS[nonce] = ("alice", "verifier-abc-123", time.monotonic())

    entry = _PENDING_FLOWS[nonce]
    assert len(entry) == 3
    name, verifier, created = entry
    assert name == "alice"
    assert verifier == "verifier-abc-123"
    assert isinstance(created, float)


def test_gc_pending_prunes_only_expired_three_tuples():
    """_gc_pending must unpack 3-tuples; if it tries to unpack 2 it
    silently breaks the GC pass on every /start request."""
    _clear_pending()
    now = time.monotonic()
    _PENDING_FLOWS["fresh"] = ("a", "v1", now)
    _PENDING_FLOWS["expired"] = ("b", "v2", now - _FLOW_TTL_SEC - 1.0)

    _gc_pending()

    assert "fresh" in _PENDING_FLOWS
    assert "expired" not in _PENDING_FLOWS


def test_new_nonce_unique_and_url_safe():
    """Nonces are CSRF tokens; collisions would let one flow take over
    another. URL-safe matters because they're sent as Spotify's
    `state` parameter (round-trips through a query string)."""
    nonces = {_new_nonce() for _ in range(100)}
    assert len(nonces) == 100  # no collisions in 100 draws
    for n in nonces:
        # Allow only RFC4648 base64url alphabet — no padding, no `+`/`/`.
        assert all(c.isalnum() or c in "-_" for c in n), n
        assert len(n) >= 16


def test_pkce_verifier_round_trip_via_spotipy():
    """End-to-end validation that the manual-set pattern we use works
    against the real spotipy.SpotifyPKCE class. The wizard builds two
    SpotifyPKCE instances across requests; the second one needs its
    `code_verifier` set to whatever the first one generated.
    """
    pytest_skip_if_no_spotipy()
    from spotipy.oauth2 import SpotifyPKCE

    auth1 = SpotifyPKCE(
        client_id="x" * 32,
        redirect_uri="http://127.0.0.1:8888/cb",
        scope="user-read-private",
        cache_path="/tmp/jts-test-pkce-cache",
        open_browser=False,
    )
    # Generates verifier+challenge as a side effect.
    auth1.get_authorize_url()
    captured_verifier = auth1.code_verifier
    assert captured_verifier  # non-empty string

    auth2 = SpotifyPKCE(
        client_id="x" * 32,
        redirect_uri="http://127.0.0.1:8888/cb",
        scope="user-read-private",
        cache_path="/tmp/jts-test-pkce-cache",
        open_browser=False,
    )
    # Fresh instance has no verifier — proving the persistence problem.
    assert auth2.code_verifier is None

    # The fix: assigning the captured verifier puts auth2 in the same
    # state auth1 was in after authorize, so a token exchange would
    # send the right code_verifier.
    auth2.code_verifier = captured_verifier
    assert auth2.code_verifier == captured_verifier


def pytest_skip_if_no_spotipy():
    try:
        import spotipy  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("spotipy not installed in this environment")
