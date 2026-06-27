# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

from jasper.accounts import Account
from jasper.spotify_router import AccountClient, Router


def _ac(name: str, *, title: str | None = None, is_playing: bool = False) -> AccountClient:
    """Build a fake AccountClient. `title` is what current_playback returns
    for item.name; None means current_playback returns None."""
    sp = MagicMock()
    if title is None:
        sp.current_playback = MagicMock(return_value=None)
    else:
        sp.current_playback = MagicMock(return_value={
            "is_playing": is_playing,
            "item": {"name": title},
        })
    return AccountClient(account=Account(name=name), sp=sp)


# --- resolve_for_transport ---


def test_resolve_for_transport_single_title_match():
    jasper = _ac("jasper", title="Hey Jude", is_playing=True)
    brittany = _ac("brittany", title="Float On", is_playing=False)
    r = Router(clients={"jasper": jasper, "brittany": brittany}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Hey Jude"))
    assert result is jasper


def test_resolve_for_transport_no_match_returns_none():
    jasper = _ac("jasper", title="Hey Jude")
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Float On"))
    assert result is None


def test_resolve_for_transport_normalizes_title():
    """Punctuation/case differences between MPRIS title and Spotify
    canonical name shouldn't kill the match — same _normalise the
    AirPlay→Spotify resolver already uses."""
    jasper = _ac("jasper", title="Hey Jude")
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's iPhone", "HEY JUDE!"))
    assert result is jasper


def test_resolve_for_transport_paused_account_still_matches():
    """User paused mid-AirPlay then says 'next' — current_playback
    still has the right item.name even with is_playing=false."""
    jasper = _ac("jasper", title="Hey Jude", is_playing=False)
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's iPhone", "Hey Jude"))
    assert result is jasper


def test_resolve_for_transport_multi_match_prefers_is_playing():
    """Both accounts queued the same track, but only one is actively
    playing — that one is the AirPlay sender."""
    jasper = _ac("jasper", title="Hey Jude", is_playing=False)
    brittany = _ac("brittany", title="Hey Jude", is_playing=True)
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="jasper",
    )
    result = asyncio.run(r.resolve_for_transport("Brittany's iPhone", "Hey Jude"))
    assert result is brittany


def test_resolve_for_transport_multi_match_falls_back_to_default():
    """Both accounts have the same title and same is_playing state —
    can't disambiguate. Punt to default."""
    jasper = _ac("jasper", title="Hey Jude", is_playing=True)
    brittany = _ac("brittany", title="Hey Jude", is_playing=True)
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="brittany",
    )
    result = asyncio.run(r.resolve_for_transport("ambiguous", "Hey Jude"))
    assert result is brittany


def test_resolve_for_transport_caches_decision():
    """Repeated calls with the same (client_name, title) hit the cache —
    don't re-poll Spotify."""
    jasper = _ac("jasper", title="Hey Jude", is_playing=True)
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Hey Jude"))
    assert jasper.sp.current_playback.call_count == 1
    asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Hey Jude"))
    assert jasper.sp.current_playback.call_count == 1  # cache hit


def test_resolve_for_transport_invalidates_on_track_change():
    """When the AirPlay track rolls over to a new title, re-resolve
    (the new track might belong to a different account)."""
    jasper = _ac("jasper", title="Hey Jude", is_playing=True)
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Hey Jude"))
    # Track rolled over to Float On — and jasper happens to be playing it.
    jasper.sp.current_playback = MagicMock(return_value={
        "is_playing": True, "item": {"name": "Float On"},
    })
    asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Float On"))
    assert jasper.sp.current_playback.call_count == 1  # re-polled


def test_resolve_for_transport_invalidates_on_sender_change():
    jasper = _ac("jasper", title="Hey Jude", is_playing=True)
    brittany = _ac("brittany", title="Hey Jude", is_playing=True)
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="jasper",
    )
    asyncio.run(r.resolve_for_transport("Jasper's iPhone", "Hey Jude"))
    n0 = jasper.sp.current_playback.call_count + brittany.sp.current_playback.call_count
    asyncio.run(r.resolve_for_transport("Brittany's iPhone", "Hey Jude"))
    n1 = jasper.sp.current_playback.call_count + brittany.sp.current_playback.call_count
    assert n1 > n0


def test_resolve_for_transport_empty_inputs_return_none():
    jasper = _ac("jasper", title="Hey Jude")
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    assert asyncio.run(r.resolve_for_transport("", "Hey Jude")) is None
    assert asyncio.run(r.resolve_for_transport("Jasper's iPhone", "")) is None


def test_resolve_for_transport_no_clients_returns_none():
    r = Router(clients={}, default_name="")
    assert asyncio.run(r.resolve_for_transport("X", "Hey Jude")) is None


def test_invalidate_cache_clears_decision():
    jasper = _ac("jasper", title="Hey Jude", is_playing=True)
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Hey Jude"))
    r.invalidate_cache()
    asyncio.run(r.resolve_for_transport("Jasper's Mac Studio", "Hey Jude"))
    assert jasper.sp.current_playback.call_count == 2  # re-polled after invalidation


def test_resolve_for_transport_retries_on_transient_none():
    """First call: current_playback returns None (transient blip).
    Second call: returns real data with matching title. Resolver should
    retry and find the match instead of returning None on the first
    miss."""
    jasper = AccountClient(account=Account(name="jasper"), sp=MagicMock())
    jasper.sp.current_playback = MagicMock(side_effect=[
        None,  # attempt 1: API hiccup
        {"is_playing": True, "item": {"name": "Hey Jude"}},  # attempt 2: real
    ])
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's Mac", "Hey Jude"))
    assert result is jasper
    assert jasper.sp.current_playback.call_count == 2


def test_resolve_for_transport_retries_on_exception():
    """API raised on first call, succeeded on retry. Should still
    resolve."""
    jasper = AccountClient(account=Account(name="jasper"), sp=MagicMock())
    jasper.sp.current_playback = MagicMock(side_effect=[
        RuntimeError("connection reset"),
        {"is_playing": True, "item": {"name": "Hey Jude"}},
    ])
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's Mac", "Hey Jude"))
    assert result is jasper


def test_resolve_for_transport_no_retry_when_data_but_no_match():
    """Account returned real playback data but title didn't match — that
    means non-Spotify AirPlay sender. Don't retry; let caller fall
    through to DACP fast."""
    jasper = AccountClient(account=Account(name="jasper"), sp=MagicMock())
    jasper.sp.current_playback = MagicMock(return_value={
        "is_playing": True, "item": {"name": "Some Other Track"},
    })
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's Mac", "Apple Music Track"))
    assert result is None
    assert jasper.sp.current_playback.call_count == 1  # no retry


def test_resolve_for_transport_gives_up_after_retry_budget():
    """All 3 attempts return None. Resolver should give up after the
    retry budget is exhausted, not loop forever."""
    jasper = AccountClient(account=Account(name="jasper"), sp=MagicMock())
    jasper.sp.current_playback = MagicMock(return_value=None)
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    result = asyncio.run(r.resolve_for_transport("Jasper's Mac", "Hey Jude"))
    assert result is None
    # 1 initial + 2 retries = 3 calls
    assert jasper.sp.current_playback.call_count == 3


# --- active() — cold-start path ---


def _ac_isplaying(name: str, is_playing: bool) -> AccountClient:
    sp = MagicMock()
    sp.current_playback = MagicMock(
        return_value={"is_playing": is_playing} if is_playing else None
    )
    return AccountClient(account=Account(name=name), sp=sp)


def test_active_returns_first_is_playing_account():
    jasper = _ac_isplaying("jasper", is_playing=False)
    brittany = _ac_isplaying("brittany", is_playing=True)
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="jasper",
    )
    assert asyncio.run(r.active(airplay_active=False)) is brittany


def test_active_falls_back_to_default():
    jasper = _ac_isplaying("jasper", is_playing=False)
    brittany = _ac_isplaying("brittany", is_playing=False)
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="brittany",
    )
    assert asyncio.run(r.active(airplay_active=False)) is brittany


def test_active_returns_none_with_no_clients():
    r = Router(clients={}, default_name="")
    assert asyncio.run(r.active(airplay_active=False)) is None


# --- build_clients return value: BuildResult with per-account statuses ---


def test_build_clients_returns_build_result_shape():
    """Smoke test: build_clients now returns a BuildResult with
    `clients` and `statuses` fields. The old return shape (bare dict)
    is gone; callers that haven't migrated will break loudly."""
    from jasper.spotify_router import BuildResult, build_clients
    from jasper.accounts import Registry
    empty_registry = Registry(accounts=[], default_name="")
    result = build_clients(empty_registry, client_id="abc", redirect_uri="x")
    assert isinstance(result, BuildResult)
    assert result.clients == {}
    assert result.statuses == []


def test_build_clients_marks_missing_cache_as_needs_oauth(tmp_path):
    """Account registered but cache file doesn't exist on disk → status
    is 'needs_oauth', not 'revoked'. The wizard uses this to render
    'not linked' vs 'session expired' badges."""
    from jasper.spotify_router import (
        ACCOUNT_NEEDS_OAUTH, build_clients,
    )
    from jasper.accounts import Account, Registry
    registry = Registry(
        accounts=[Account(name="ghost", cache_path=str(tmp_path / "missing.json"))],
        default_name="ghost",
    )
    result = build_clients(registry, client_id="abc", redirect_uri="x")
    assert result.clients == {}
    assert len(result.statuses) == 1
    assert result.statuses[0].name == "ghost"
    assert result.statuses[0].state == ACCOUNT_NEEDS_OAUTH


def test_classify_oauth_error_revoked_vs_other():
    """The revoked-vs-error classifier drives the user-facing message.
    Any error whose `.error` attr is 'invalid_grant' or whose rendered
    text contains 'invalid_grant'/'revoked' maps to revoked; everything
    else falls through to error."""
    from jasper.spotify_router import (
        ACCOUNT_ERROR, ACCOUNT_REVOKED, _classify_oauth_error,
    )
    state, _ = _classify_oauth_error(
        Exception("error: invalid_grant, error_description: Refresh token revoked"),
    )
    assert state == ACCOUNT_REVOKED
    state, _ = _classify_oauth_error(Exception("Connection timed out"))
    assert state == ACCOUNT_ERROR
    state, _ = _classify_oauth_error(Exception("HTTP 500 from Spotify"))
    assert state == ACCOUNT_ERROR


def test_classify_oauth_error_inspects_error_attr_for_spotipy_exceptions():
    """spotipy's SpotifyOauthError carries structured `.error` and
    `.error_description` attributes. Inspect `.error` directly so a
    future spotipy refactor of the str() format doesn't silently
    reclassify revoked → error and break the user-facing message."""
    from jasper.spotify_router import ACCOUNT_REVOKED, _classify_oauth_error

    class FakeSpotifyOauthError(Exception):
        # Mirrors spotipy's SpotifyOauthError shape.
        def __init__(self, msg: str, error: str = "", error_description: str = ""):
            super().__init__(msg)
            self.error = error
            self.error_description = error_description

    # str() is empty but the structured attr is set — this is the case
    # a substring-only classifier would misclassify.
    exc = FakeSpotifyOauthError("", error="invalid_grant",
                                error_description="Refresh token revoked")
    state, detail = _classify_oauth_error(exc)
    assert state == ACCOUNT_REVOKED
    assert "re-link" in detail.lower()


# --- Router.refresh_if_empty + empty_reason ---


def test_router_refresh_if_empty_populates_from_rebuild_fn():
    """When clients dict is empty and a rebuild_fn is set, refresh_if_empty
    runs the rebuild and atomically replaces clients + statuses. This
    is what lets a wizard re-link recover the daemon without a restart."""
    from jasper.spotify_router import (
        ACCOUNT_OK, AccountStatus, BuildResult,
    )
    rebuilt_ac = _ac("jasper", title="Hey Jude")

    def rebuild():
        return BuildResult(
            clients={"jasper": rebuilt_ac},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
        )

    r = Router(clients={}, default_name="jasper", rebuild_fn=rebuild)
    assert asyncio.run(r.refresh_if_empty()) is True
    assert "jasper" in r.clients
    assert r.clients["jasper"] is rebuilt_ac
    assert r.statuses[0].state == ACCOUNT_OK


def test_router_refresh_if_empty_no_op_when_clients_present():
    """The fast path: when clients is already non-empty, refresh_if_empty
    returns True without calling rebuild_fn — every voice command goes
    through this check, so it has to be cheap on the happy path."""
    from jasper.spotify_router import BuildResult
    calls = {"n": 0}
    def rebuild():
        calls["n"] += 1
        return BuildResult(clients={}, statuses=[])
    ac = _ac("jasper", title="Hey Jude")
    r = Router(clients={"jasper": ac}, default_name="jasper", rebuild_fn=rebuild)
    assert asyncio.run(r.refresh_if_empty()) is True
    assert calls["n"] == 0


def test_router_refresh_if_empty_rate_limited():
    """When a token is persistently revoked, every voice command would
    otherwise hammer the rebuild path → one HTTP refresh attempt per
    voice turn. Rate-limit blocks repeat rebuild calls inside the
    cooldown window. Uses a controlled `time.monotonic` so the test is
    deterministic regardless of CI scheduling."""
    from unittest.mock import patch
    from jasper.spotify_router import (
        ACCOUNT_REVOKED, AccountStatus, BuildResult,
    )
    calls = {"n": 0}
    def rebuild():
        calls["n"] += 1
        return BuildResult(
            clients={},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_REVOKED)],
        )
    r = Router(clients={}, default_name="jasper", rebuild_fn=rebuild)
    # Three rapid calls inside the cooldown window: only the first
    # should fire the rebuild_fn; the next two are throttled.
    times = iter([1000.0, 1005.0, 1015.0])
    with patch("jasper.spotify_router._now", side_effect=lambda: next(times)):
        asyncio.run(r.refresh_if_empty())
        asyncio.run(r.refresh_if_empty())
        asyncio.run(r.refresh_if_empty())
    assert calls["n"] == 1


def test_router_refresh_if_empty_retries_after_cooldown_window():
    """The flip side of rate-limiting: after the cooldown elapses, the
    next attempt actually fires. A one-shot lockout bug (e.g. wrong
    comparison operator) would pass the rate-limit test above but fail
    here."""
    from unittest.mock import patch
    from jasper.spotify_router import (
        ACCOUNT_REVOKED, AccountStatus, BuildResult,
        _REFRESH_MIN_INTERVAL_SEC,
    )
    calls = {"n": 0}
    def rebuild():
        calls["n"] += 1
        return BuildResult(
            clients={},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_REVOKED)],
        )
    r = Router(clients={}, default_name="jasper", rebuild_fn=rebuild)
    times = iter([
        1000.0,                                  # first attempt
        1000.0 + _REFRESH_MIN_INTERVAL_SEC + 1.0,  # past cooldown
    ])
    with patch("jasper.spotify_router._now", side_effect=lambda: next(times)):
        asyncio.run(r.refresh_if_empty())
        asyncio.run(r.refresh_if_empty())
    assert calls["n"] == 2


def test_router_refresh_if_empty_swallows_rebuild_exception_and_keeps_cooldown_open():
    """Transient failures (network blip during build_clients, OSError
    reading the registry, etc.) must NOT advance the cooldown — that
    would lock the user out of recovery for _REFRESH_MIN_INTERVAL_SEC
    over a problem that may already be gone."""
    calls = {"n": 0}
    def rebuild():
        calls["n"] += 1
        raise RuntimeError("transient network failure")
    r = Router(clients={}, default_name="jasper", rebuild_fn=rebuild)
    assert asyncio.run(r.refresh_if_empty()) is False
    # Immediately retry — should fire again, not be throttled, because
    # the previous attempt raised.
    assert asyncio.run(r.refresh_if_empty()) is False
    assert calls["n"] == 2
    assert r.clients == {}
    assert r.statuses == []


def test_router_refresh_if_empty_no_rebuild_fn_returns_false_without_side_effects():
    """A Router constructed without a rebuild_fn (e.g. mux.py / control
    daemon construct one-shot Routers) must return False cleanly when
    asked to refresh — no AttributeError, no state mutation."""
    r = Router(clients={}, default_name="jasper")
    assert asyncio.run(r.refresh_if_empty()) is False
    assert r._last_refresh_attempt is None


def test_router_refresh_if_empty_updates_default_name_on_rebuild():
    """When the wizard changes the default account (POST /default),
    the rebuild_fn surfaces the new default via BuildResult.default_name.
    Router.refresh_if_empty must mirror it onto self.default_name so
    subsequent active() calls route to the new default."""
    from jasper.spotify_router import (
        ACCOUNT_OK, AccountStatus, BuildResult,
    )
    rebuilt_ac = _ac("brittany", title="Hey Jude")

    def rebuild():
        return BuildResult(
            clients={"brittany": rebuilt_ac},
            statuses=[AccountStatus(name="brittany", state=ACCOUNT_OK)],
            default_name="brittany",
        )

    r = Router(clients={}, default_name="jasper", rebuild_fn=rebuild)
    assert asyncio.run(r.refresh_if_empty()) is True
    assert r.default_name == "brittany"


def test_router_refresh_if_empty_propagates_statuses_even_when_clients_still_empty():
    """A failed rebuild still surfaces per-account statuses so the tool
    layer can render the right error message (revoked vs needs_oauth)."""
    from jasper.spotify_router import (
        ACCOUNT_REVOKED, AccountStatus, BuildResult,
    )
    def rebuild():
        return BuildResult(
            clients={},
            statuses=[AccountStatus(
                name="jasper", state=ACCOUNT_REVOKED, detail="revoked",
            )],
        )
    r = Router(clients={}, default_name="jasper", rebuild_fn=rebuild)
    assert asyncio.run(r.refresh_if_empty()) is False
    assert r.statuses[0].state == ACCOUNT_REVOKED


def test_router_empty_reason_returns_empty_when_clients_present():
    ac = _ac("jasper", title="Hey Jude")
    r = Router(clients={"jasper": ac}, default_name="jasper")
    assert r.empty_reason() == ""


def test_router_empty_reason_revoked_when_any_status_revoked():
    from jasper.spotify_router import ACCOUNT_REVOKED, AccountStatus
    r = Router(
        clients={},
        default_name="jasper",
        statuses=[AccountStatus(name="jasper", state=ACCOUNT_REVOKED)],
    )
    assert r.empty_reason() == "revoked"


def test_router_empty_reason_no_accounts_when_no_statuses():
    r = Router(clients={}, default_name="")
    assert r.empty_reason() == "no_accounts"


def test_router_empty_reason_needs_oauth_for_all_unauthed_accounts():
    from jasper.spotify_router import ACCOUNT_NEEDS_OAUTH, AccountStatus
    r = Router(
        clients={},
        default_name="jasper",
        statuses=[AccountStatus(name="jasper", state=ACCOUNT_NEEDS_OAUTH)],
    )
    assert r.empty_reason() == "needs_oauth"


def test_router_revoked_account_names_filters_to_revoked_only():
    """The voice tool reads this to name the affected accounts in the
    spoken error. Must include only ACCOUNT_REVOKED entries — not
    ACCOUNT_OK or ACCOUNT_NEEDS_OAUTH (those don't need re-linking)."""
    from jasper.spotify_router import (
        ACCOUNT_NEEDS_OAUTH, ACCOUNT_OK, ACCOUNT_REVOKED, AccountStatus,
    )
    r = Router(
        clients={},
        default_name="jasper",
        statuses=[
            AccountStatus(name="jasper", state=ACCOUNT_OK),
            AccountStatus(name="brittany", state=ACCOUNT_REVOKED),
            AccountStatus(name="guest", state=ACCOUNT_NEEDS_OAUTH),
            AccountStatus(name="alice", state=ACCOUNT_REVOKED),
        ],
    )
    assert r.revoked_account_names() == ["brittany", "alice"]


def test_router_revoked_account_names_empty_when_no_revoked():
    from jasper.spotify_router import ACCOUNT_OK, AccountStatus
    r = Router(
        clients={"jasper": _ac("jasper")},
        default_name="jasper",
        statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
    )
    assert r.revoked_account_names() == []


# --- Real-spotipy integration: the exact bug PR #162 fixed ---


def test_build_clients_classifies_invalid_grant_from_real_spotipy(tmp_path):
    """End-to-end pin for the production bug: spotipy's SpotifyPKCE,
    given a cache file with an expired access_token + revoked
    refresh_token, attempts the refresh and Spotify's auth server
    returns HTTP 400 + invalid_grant. build_clients must classify
    that as ACCOUNT_REVOKED and skip the client.

    Mocks spotipy at the HTTP transport layer (requests.Session.post)
    rather than at any spotipy boundary, so this would catch a
    regression even if spotipy refactored its internal exception
    handling. This is the test that would have caught the original
    production bug end-to-end."""
    import json
    from unittest.mock import MagicMock, patch
    from jasper.spotify_router import ACCOUNT_REVOKED, build_clients
    from jasper.accounts import Account, Registry

    # 1. Plant a cache file that looks like a real spotipy PKCE cache
    #    with an expired access_token + a refresh_token.
    cache_path = tmp_path / "jasper.json"
    cache_path.write_text(json.dumps({
        "access_token": "expired_access_token_value",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "the_revoked_refresh_token",
        "scope": (
            "user-modify-playback-state user-read-playback-state "
            "user-read-currently-playing user-read-private "
            "playlist-read-private playlist-read-collaborative"
        ),
        "expires_at": 0,  # in the past → spotipy will attempt refresh
    }))
    registry = Registry(
        accounts=[Account(name="jasper", cache_path=str(cache_path))],
        default_name="jasper",
    )

    # 2. Mock the HTTP POST that spotipy's refresh_access_token makes.
    #    Spotify returns 400 + {"error": "invalid_grant", ...}.
    import requests

    def fake_post(*args, **kwargs):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = '{"error": "invalid_grant", "error_description": "Refresh token revoked"}'
        resp.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Refresh token revoked",
        }
        err = requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status.side_effect = err
        return resp

    with patch("spotipy.oauth2.requests.Session.post", side_effect=fake_post):
        result = build_clients(
            registry,
            client_id="a" * 32,  # valid-looking PKCE client_id
            redirect_uri="https://example.com/cb",
        )

    # 3. Account should be classified revoked, not OK or generic-error.
    assert result.clients == {}, (
        "revoked refresh_token should not produce a usable client"
    )
    assert len(result.statuses) == 1
    assert result.statuses[0].name == "jasper"
    assert result.statuses[0].state == ACCOUNT_REVOKED, (
        f"expected REVOKED, got {result.statuses[0].state} "
        f"(detail={result.statuses[0].detail!r})"
    )


def test_build_clients_passes_requests_timeout_to_spotipy(tmp_path):
    """spotipy's default is no requests_timeout, so a hung Spotify API
    socket would block the calling thread indefinitely (and, via mux's
    inline-awaited pause loop, the whole mux tick). build_clients must
    construct spotipy.Spotify with a bounded requests_timeout."""
    from unittest.mock import MagicMock, patch
    from jasper.spotify_router import (
        _SPOTIPY_REQUESTS_TIMEOUT_SEC, build_clients,
    )
    from jasper.accounts import Account, Registry

    cache_path = tmp_path / "j.json"
    cache_path.write_text("{}")  # must exist; SpotifyPKCE is mocked below
    registry = Registry(
        accounts=[Account(name="jasper", cache_path=str(cache_path))],
        default_name="jasper",
    )

    captured: dict = {}

    def fake_spotify(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    fake_auth = MagicMock()
    fake_auth.get_cached_token.return_value = {"access_token": "tok"}

    with patch("spotipy.oauth2.SpotifyPKCE", return_value=fake_auth), \
            patch("spotipy.Spotify", side_effect=fake_spotify):
        result = build_clients(
            registry, client_id="a" * 32, redirect_uri="https://x/cb",
        )

    assert "jasper" in result.clients, "usable token should build a client"
    assert captured.get("requests_timeout") == _SPOTIPY_REQUESTS_TIMEOUT_SEC, (
        "spotipy.Spotify must be built with a bounded requests_timeout so "
        "a hung API socket can't block the caller forever"
    )


def test_build_clients_dedupes_persistent_account_warnings(
    tmp_path, monkeypatch, caplog,
):
    """A revoked token is persistent account state. Rebuilding clients
    repeatedly inside one daemon should warn once, then demote repeats so the
    flight recorder and journal don't get spammed by dashboard polling."""
    from unittest.mock import patch
    from jasper import spotify_router as router_mod
    from jasper.spotify_router import build_clients
    from jasper.accounts import Account, Registry

    cache_path = tmp_path / "jasper.json"
    cache_path.write_text("{}")
    registry = Registry(
        accounts=[Account(name="jasper", cache_path=str(cache_path))],
        default_name="jasper",
    )
    fake_auth = MagicMock()
    fake_auth.get_cached_token.side_effect = Exception("invalid_grant")
    monkeypatch.setattr(router_mod, "_ACCOUNT_FAILURE_LOG_CACHE", {})
    caplog.set_level(logging.DEBUG, logger="jasper.spotify_router")

    with patch("spotipy.oauth2.SpotifyPKCE", return_value=fake_auth), \
            patch("jasper.spotify_router._now", side_effect=[1000.0, 1005.0]):
        build_clients(registry, client_id="a" * 32, redirect_uri="https://x/cb")
        build_clients(registry, client_id="a" * 32, redirect_uri="https://x/cb")

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.message.startswith("event=spotify.account_unavailable ")
    ]
    suppressed = [
        r for r in caplog.records
        if r.message.startswith("event=spotify.account_unavailable_suppressed ")
    ]
    assert len(warnings) == 1
    assert len(suppressed) == 1


# --- _ACCOUNT_FAILURE_LOG_CACHE eviction on recovery ---


def test_failure_log_cache_evicted_on_recovery(tmp_path, monkeypatch, caplog):
    """Core invariant: when an account transitions back to ACCOUNT_OK,
    its failure-log entries are evicted so a future failure logs at WARNING
    again rather than being silently suppressed as a DEBUG repeat.

    Sequence:
      1. build_clients → failure (revoked) → WARNING logged, key cached
      2. build_clients → failure again → suppressed (DEBUG, key cached)
      3. build_clients → SUCCESS (ok) → cache evicted for this account
      4. build_clients → failure again → WARNING logged again (not suppressed)

    Mutation check: removing the _evict_failure_log_cache call from the
    ACCOUNT_OK path breaks step 4 — the failure is still suppressed (DEBUG)
    even though the account recovered and failed again."""
    import json
    from unittest.mock import patch, MagicMock
    from jasper import spotify_router as router_mod
    from jasper.spotify_router import build_clients, ACCOUNT_OK
    from jasper.accounts import Account, Registry

    # A real-looking token cache so SpotifyPKCE reads it successfully
    # when we want the "ok" path.
    good_token = {
        "access_token": "valid_token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "valid_refresh",
        "scope": router_mod.SPOTIFY_SCOPE,
        "expires_at": 9_999_999_999,  # far future
    }
    cache_path = tmp_path / "jasper.json"
    registry = Registry(
        accounts=[Account(name="jasper", cache_path=str(cache_path))],
        default_name="jasper",
    )
    monkeypatch.setattr(router_mod, "_ACCOUNT_FAILURE_LOG_CACHE", {})
    caplog.set_level(logging.DEBUG, logger="jasper.spotify_router")

    failing_auth = MagicMock()
    failing_auth.get_cached_token.side_effect = Exception("invalid_grant")

    ok_auth = MagicMock()
    ok_auth.get_cached_token.return_value = good_token

    fake_spotify = MagicMock()

    # Step 1: first failure → WARNING
    cache_path.write_text("{}")  # file must exist for the cache-path check
    with patch("spotipy.oauth2.SpotifyPKCE", return_value=failing_auth), \
            patch("jasper.spotify_router._now", return_value=1000.0):
        build_clients(registry, client_id="a" * 32, redirect_uri="https://x/cb")

    # Step 2: second failure → suppressed (still within the interval)
    with patch("spotipy.oauth2.SpotifyPKCE", return_value=failing_auth), \
            patch("jasper.spotify_router._now", return_value=1005.0):
        build_clients(registry, client_id="a" * 32, redirect_uri="https://x/cb")

    # Step 3: account recovers → cache evicted
    cache_path.write_text(json.dumps(good_token))
    with patch("spotipy.oauth2.SpotifyPKCE", return_value=ok_auth), \
            patch("spotipy.Spotify", return_value=fake_spotify), \
            patch("jasper.spotify_router._now", return_value=1010.0):
        result = build_clients(registry, client_id="a" * 32, redirect_uri="https://x/cb")
    assert "jasper" in result.clients, "account should be ok after recovery"
    assert result.statuses[0].state == ACCOUNT_OK

    # Verify the cache was cleared for this account
    assert not any(
        k[0] == "jasper" for k in router_mod._ACCOUNT_FAILURE_LOG_CACHE
    ), "recovery must evict all failure-log keys for the account"

    # Step 4: account fails again after recovery → must log WARNING, not suppressed
    caplog.clear()
    cache_path.write_text("{}")
    with patch("spotipy.oauth2.SpotifyPKCE", return_value=failing_auth), \
            patch("jasper.spotify_router._now", return_value=1015.0):
        build_clients(registry, client_id="a" * 32, redirect_uri="https://x/cb")

    post_recovery_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.message.startswith("event=spotify.account_unavailable ")
    ]
    post_recovery_suppressed = [
        r for r in caplog.records
        if r.message.startswith("event=spotify.account_unavailable_suppressed ")
    ]
    assert len(post_recovery_warnings) == 1, (
        "failure after recovery must log at WARNING again — cache was not evicted"
    )
    assert len(post_recovery_suppressed) == 0


def test_failure_log_cache_eviction_does_not_affect_other_accounts(
    tmp_path, monkeypatch,
):
    """Evicting one account's failure-log entries on recovery must not
    clear entries for other accounts — those other accounts may still
    be in a failure state and their dedup should be preserved."""
    from jasper import spotify_router as router_mod
    from jasper.spotify_router import _evict_failure_log_cache

    # Seed the cache with entries for two accounts
    monkeypatch.setattr(router_mod, "_ACCOUNT_FAILURE_LOG_CACHE", {
        ("jasper", "revoked", "refresh token revoked — re-link required", ""): 1000.0,
        ("jasper", "error", "network error", ""): 1001.0,
        ("brittany", "revoked", "refresh token revoked — re-link required", ""): 1002.0,
    })

    # Recover jasper → only jasper's entries evicted
    _evict_failure_log_cache("jasper")

    remaining = list(router_mod._ACCOUNT_FAILURE_LOG_CACHE.keys())
    assert all(k[0] == "brittany" for k in remaining), (
        "eviction of 'jasper' must leave 'brittany' entries intact"
    )
    assert len(remaining) == 1
