"""Tests for the Spotify-account setup wizard's PKCE flow plumbing.

The wizard's OAuth flow spans two separate HTTP requests: /start (which
generates the PKCE verifier and authorize URL) and /oauth-callback or
/paste-callback (which exchanges the code for a token). spotipy's
SpotifyPKCE generates the verifier+challenge lazily inside
`get_authorize_url()` and stores them on the instance only. A fresh
SpotifyPKCE in the callback handler has neither, and `get_access_token`
regenerates BOTH if either is None — so the verifier you carefully
restored gets clobbered, the new verifier doesn't match the challenge
Spotify already saw, and the exchange fails with
`invalid_grant: code_verifier was incorrect`.

Two production bugs caught here:

  1. (2026-05-09 morning) The wizard didn't persist the verifier at
     all. Every OAuth attempt 400'd silently behind a status banner.

  2. (2026-05-09 afternoon) The wizard persisted the verifier but not
     the challenge. spotipy's regeneration guard fired and clobbered
     both, even though the verifier was set. Same 400 from Spotify.

The test_pkce_exchange_uses_restored_verifier check exercises the
actual exchange POST with a mocked HTTP transport — that's the level
of test that would have caught both bugs pre-deploy. The narrower
attribute-set tests are kept as cheap shape regressions.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from jasper.web.spotify_setup import (
    _FLOW_TTL_SEC,
    _PENDING_FLOWS,
    _gc_pending,
    _new_nonce,
)


def _clear_pending():
    _PENDING_FLOWS.clear()


def test_pending_flows_stores_four_tuple():
    """{nonce: (account_name, verifier, challenge, created_monotonic)}.

    Both verifier AND challenge must be stored. Storing only the
    verifier triggers spotipy's PKCE-handshake regeneration on
    exchange and breaks every OAuth flow.
    """
    _clear_pending()
    nonce = _new_nonce()
    _PENDING_FLOWS[nonce] = (
        "alice", "verifier-abc-123", "challenge-xyz-987", time.monotonic(),
    )

    entry = _PENDING_FLOWS[nonce]
    assert len(entry) == 4
    name, verifier, challenge, created = entry
    assert name == "alice"
    assert verifier == "verifier-abc-123"
    assert challenge == "challenge-xyz-987"
    assert isinstance(created, float)


def test_gc_pending_prunes_only_expired_entries():
    """_gc_pending must unpack 4-tuples; if the shape is wrong it
    breaks GC silently and stale entries leak."""
    _clear_pending()
    now = time.monotonic()
    _PENDING_FLOWS["fresh"] = ("a", "v1", "c1", now)
    _PENDING_FLOWS["expired"] = ("b", "v2", "c2", now - _FLOW_TTL_SEC - 1.0)

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
        # RFC4648 base64url alphabet — no padding, no `+`/`/`.
        assert all(c.isalnum() or c in "-_" for c in n), n
        assert len(n) >= 16


def _spotipy_or_skip():
    try:
        import spotipy  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("spotipy not installed in this environment")


def test_pkce_exchange_uses_restored_verifier():
    """End-to-end: build SpotifyPKCE, capture verifier+challenge, build
    a NEW SpotifyPKCE (simulating cross-request), restore both, mock
    the HTTP transport, call get_access_token, and assert that the
    verifier in the POST payload is the ORIGINAL one (not a regenerated
    value).

    This is the test that would have caught both production bugs:
    the original "didn't persist anything" miss and the second
    "persisted only the verifier" miss. Setting only code_verifier
    leaves code_challenge=None, which trips spotipy's regeneration
    guard inside get_access_token and clobbers our verifier.
    """
    _spotipy_or_skip()
    from spotipy.oauth2 import SpotifyPKCE

    auth1 = SpotifyPKCE(
        client_id="x" * 32,
        redirect_uri="http://127.0.0.1:8888/cb",
        scope="user-read-private",
        cache_path="/tmp/jts-test-pkce-1",
        open_browser=False,
    )
    auth1.get_authorize_url()
    captured_verifier = auth1.code_verifier
    captured_challenge = auth1.code_challenge
    assert captured_verifier and captured_challenge

    auth2 = SpotifyPKCE(
        client_id="x" * 32,
        redirect_uri="http://127.0.0.1:8888/cb",
        scope="user-read-private",
        cache_path="/tmp/jts-test-pkce-2",
        open_browser=False,
    )
    assert auth2.code_verifier is None
    assert auth2.code_challenge is None

    # The fix: restore BOTH halves on the new instance before exchange.
    auth2.code_verifier = captured_verifier
    auth2.code_challenge = captured_challenge

    sent_payload: dict = {}

    def fake_post(url, data=None, headers=None, **kw):
        sent_payload.update(data or {})
        resp = MagicMock()
        resp.json.return_value = {
            "access_token": "fake-access",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "fake-refresh",
            "scope": "user-read-private",
        }
        resp.raise_for_status.return_value = None
        return resp

    # Also avoid touching the real cache file when spotipy tries to
    # save the token after a "successful" exchange.
    auth2.cache_handler = MagicMock()

    with patch.object(auth2._session, "post", side_effect=fake_post):
        auth2.get_access_token(code="dummy-auth-code", check_cache=False)

    # Critical assertion: spotipy sent OUR captured verifier in the
    # exchange POST. If spotipy's guard had regenerated, this would
    # be a different value.
    assert sent_payload["code_verifier"] == captured_verifier, (
        "spotipy regenerated the PKCE handshake — the verifier we "
        "restored was clobbered. The fix needs to set BOTH "
        "code_verifier and code_challenge on the SpotifyPKCE instance "
        "before calling get_access_token."
    )
    assert sent_payload["grant_type"] == "authorization_code"
    assert sent_payload["code"] == "dummy-auth-code"


def test_setting_only_verifier_is_clobbered_by_spotipy():
    """Negative test: confirms that the obvious-but-wrong fix (setting
    just `code_verifier` and leaving `code_challenge=None`) does NOT
    work. Pinning this behaviour so a future cleanup pass doesn't
    "simplify" the code back into the broken state.
    """
    _spotipy_or_skip()
    from spotipy.oauth2 import SpotifyPKCE

    auth1 = SpotifyPKCE(
        client_id="x" * 32, redirect_uri="http://127.0.0.1:8888/cb",
        scope="user-read-private", cache_path="/tmp/jts-test-pkce-3",
        open_browser=False,
    )
    auth1.get_authorize_url()
    captured_verifier = auth1.code_verifier

    auth2 = SpotifyPKCE(
        client_id="x" * 32, redirect_uri="http://127.0.0.1:8888/cb",
        scope="user-read-private", cache_path="/tmp/jts-test-pkce-4",
        open_browser=False,
    )
    auth2.code_verifier = captured_verifier
    # Deliberately leave code_challenge=None.

    sent_payload: dict = {}

    def fake_post(url, data=None, headers=None, **kw):
        sent_payload.update(data or {})
        resp = MagicMock()
        resp.json.return_value = {
            "access_token": "x", "token_type": "Bearer", "expires_in": 3600,
            "refresh_token": "x", "scope": "user-read-private",
        }
        resp.raise_for_status.return_value = None
        return resp

    auth2.cache_handler = MagicMock()
    with patch.object(auth2._session, "post", side_effect=fake_post):
        auth2.get_access_token(code="dummy", check_cache=False)

    # Spotipy regenerated the handshake — verifier in the payload is
    # NOT the one we set. This is the bug we're guarding against.
    assert sent_payload["code_verifier"] != captured_verifier


# ---------------------------------------------------------------------
# Token-health probe + rendering
#
# The /spotify page used to claim "configured" purely on file presence.
# When Spotify revoked the refresh token (password change, security
# sweep, supersede-by-newer-grant), the page lied and the voice tool
# returned "no spotify account configured" with no signal to the user
# that re-linking would fix it. The probe + badges close that loop.
# ---------------------------------------------------------------------


def test_probe_all_health_caches_within_ttl(tmp_path):
    """Two consecutive renders within the TTL must not re-run
    build_clients — that would hit Spotify's token endpoint per render."""
    from jasper.web import spotify_setup as ss
    ss._invalidate_health_cache()
    calls = {"n": 0}
    fake_result = MagicMock(clients={}, statuses=[])

    def fake_build(_registry, *, client_id, redirect_uri):
        calls["n"] += 1
        return fake_result

    cfg = {
        "client_id": "abc123",
        "mode": "bounce",
        "registry_path": str(tmp_path / "registry.json"),
        "bounce_redirect_uri": "https://example.com/cb",
        "manual_redirect_uri": "http://127.0.0.1/cb",
    }
    # Empty registry so Registry.load returns something parseable.
    (tmp_path / "registry.json").write_text('{"accounts": [], "default": ""}')
    with patch.object(ss, "build_clients", side_effect=fake_build):
        r1 = ss._probe_all_health(cfg)
        r2 = ss._probe_all_health(cfg)
    assert r1 is r2
    assert calls["n"] == 1


def test_probe_all_health_no_client_id_returns_empty_without_calling_build():
    """Defensive: a credentials-less wizard render must not even try to
    call build_clients — there's nothing to authenticate against."""
    from jasper.web import spotify_setup as ss
    ss._invalidate_health_cache()
    calls = {"n": 0}

    def fake_build(*a, **kw):
        calls["n"] += 1
        return MagicMock()

    with patch.object(ss, "build_clients", side_effect=fake_build):
        result = ss._probe_all_health({
            "client_id": "", "mode": "bounce",
            "registry_path": "/tmp/x", "bounce_redirect_uri": "",
            "manual_redirect_uri": "",
        })
    assert result.clients == {} and result.statuses == []
    assert calls["n"] == 0


def test_health_badge_renders_per_state():
    """The badge string differs by state so it's instantly visible whether
    an account is healthy, expired, or unauthed."""
    from jasper.spotify_router import (
        ACCOUNT_NEEDS_OAUTH, ACCOUNT_OK, ACCOUNT_REVOKED, AccountStatus,
    )
    from jasper.web.spotify_setup import _health_badge_html
    ok_html = _health_badge_html(AccountStatus(name="x", state=ACCOUNT_OK))
    rev_html = _health_badge_html(AccountStatus(name="x", state=ACCOUNT_REVOKED))
    needs_html = _health_badge_html(
        AccountStatus(name="x", state=ACCOUNT_NEEDS_OAUTH),
    )
    assert "linked" in ok_html and "health-ok" in ok_html
    assert "session expired" in rev_html and "health-revoked" in rev_html
    assert "not linked" in needs_html and "health-warn" in needs_html
    # None status (probe disabled / failed to run) renders nothing so the
    # rest of the card stays usable.
    assert _health_badge_html(None) == ""


def test_relink_notice_only_shown_for_revoked():
    """The "Re-link" CTA must appear only when the token is revoked —
    not on healthy or not-yet-OAuthed accounts (different action)."""
    from jasper.spotify_router import (
        ACCOUNT_NEEDS_OAUTH, ACCOUNT_OK, ACCOUNT_REVOKED, AccountStatus,
    )
    from jasper.web.spotify_setup import _relink_notice_html
    revoked = _relink_notice_html(
        AccountStatus(name="jasper", state=ACCOUNT_REVOKED), "jasper",
    )
    assert "Re-link jasper" in revoked
    assert 'action="start"' in revoked
    assert _relink_notice_html(
        AccountStatus(name="x", state=ACCOUNT_OK), "x",
    ) == ""
    assert _relink_notice_html(
        AccountStatus(name="x", state=ACCOUNT_NEEDS_OAUTH), "x",
    ) == ""
    assert _relink_notice_html(None, "x") == ""
