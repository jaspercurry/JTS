from __future__ import annotations

import asyncio
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
