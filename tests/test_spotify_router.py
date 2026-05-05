from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from jasper.accounts import Account
from jasper.spotify_router import AccountClient, Router


def _ac(name: str, patterns: list[str], *, is_playing: bool = False) -> AccountClient:
    sp = MagicMock()
    sp.current_playback = MagicMock(
        return_value={"is_playing": is_playing} if is_playing else None
    )
    return AccountClient(
        account=Account(name=name, client_name_patterns=patterns),
        sp=sp,
    )


def test_resolve_airplay_returns_matching_account():
    jasper = _ac("jasper", ["Jasper's iPhone"])
    brittany = _ac("brittany", ["Brittany's iPhone"])
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="jasper",
    )
    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=AsyncMock(return_value="Brittany’s iPhone"),
    ):
        result = asyncio.run(r.resolve_airplay())
    assert result is brittany


def test_resolve_airplay_returns_none_when_no_match():
    jasper = _ac("jasper", ["Jasper's iPhone"])
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=AsyncMock(return_value="Alex's iPhone"),
    ):
        assert asyncio.run(r.resolve_airplay()) is None


def test_resolve_airplay_returns_none_when_airplay_inactive():
    jasper = _ac("jasper", ["Jasper's iPhone"])
    r = Router(clients={"jasper": jasper}, default_name="jasper")
    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=AsyncMock(return_value=""),
    ):
        assert asyncio.run(r.resolve_airplay()) is None


def test_active_prefers_airplay_match():
    jasper = _ac("jasper", ["Jasper's iPhone"])
    brittany = _ac("brittany", ["Brittany's iPhone"])
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="jasper",
    )
    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=AsyncMock(return_value="Brittany's iPhone"),
    ):
        result = asyncio.run(r.active(airplay_active=True))
    assert result is brittany


def test_active_falls_back_to_is_playing():
    jasper = _ac("jasper", ["Jasper's iPhone"], is_playing=False)
    brittany = _ac("brittany", ["Brittany's iPhone"], is_playing=True)
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="jasper",
    )
    # AirPlay inactive — no ClientName
    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=AsyncMock(return_value=""),
    ):
        result = asyncio.run(r.active(airplay_active=False))
    assert result is brittany


def test_active_falls_back_to_default():
    jasper = _ac("jasper", ["Jasper's iPhone"])
    brittany = _ac("brittany", ["Brittany's iPhone"])
    r = Router(
        clients={"jasper": jasper, "brittany": brittany},
        default_name="brittany",
    )
    with patch(
        "jasper.spotify_router.airplay_client_name",
        new=AsyncMock(return_value=""),
    ):
        result = asyncio.run(r.active(airplay_active=False))
    assert result is brittany


def test_active_returns_none_with_no_clients():
    r = Router(clients={}, default_name="")
    assert asyncio.run(r.active(airplay_active=False)) is None
