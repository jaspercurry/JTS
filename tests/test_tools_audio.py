from __future__ import annotations

import asyncio

import pytest

from jasper.tools.audio import (
    VOLUME_MAX_DB,
    VOLUME_MIN_DB,
    _db_to_percent,
    _percent_to_db,
    make_audio_tools,
)


class FakeCoordinator:
    """Stand-in for VolumeCoordinator. Records every call so tests
    can assert dispatch shape without touching real DBus/HTTP.

    Mute is faithfully modeled (saves/restores prior level) so the
    mute→unmute round-trip test continues to exercise the same
    contract."""

    def __init__(self, level: int = 50) -> None:
        self._level = int(level)
        self._pre_mute: int | None = None
        self.calls: list[tuple[str, int | None]] = []

    def get_listening_level(self) -> int:
        return self._level

    def is_muted(self) -> bool:
        return self._pre_mute is not None

    async def set_listening_level(self, percent: int) -> int:
        target = max(0, min(100, int(percent)))
        self._level = target
        self._pre_mute = None
        self.calls.append(("set", target))
        return target

    async def adjust_listening_level(self, delta: int) -> int:
        target = max(0, min(100, self._level + int(delta)))
        self._level = target
        self._pre_mute = None
        self.calls.append(("adjust", int(delta)))
        return target

    async def mute(self) -> int:
        if self._pre_mute is None and self._level > 0:
            self._pre_mute = self._level
        saved = self._pre_mute or 0
        self._level = 0
        self.calls.append(("mute", None))
        return saved

    async def unmute(self, fallback_level: int = 50) -> int:
        target = self._pre_mute if self._pre_mute is not None else fallback_level
        target = max(0, min(100, int(target)))
        self._pre_mute = None
        self._level = target
        self.calls.append(("unmute", target))
        return target


def _tools(coordinator):
    return {f.__name__: f for f in make_audio_tools(coordinator)}


def test_percent_db_round_trip_at_endpoints():
    assert _percent_to_db(0) == VOLUME_MIN_DB
    assert _percent_to_db(100) == VOLUME_MAX_DB
    assert _db_to_percent(VOLUME_MIN_DB) == 0
    assert _db_to_percent(VOLUME_MAX_DB) == 100


def test_percent_to_db_is_linear_midpoint():
    assert _percent_to_db(50) == pytest.approx((VOLUME_MIN_DB + VOLUME_MAX_DB) / 2)


def test_percent_clamped_out_of_range():
    assert _percent_to_db(-10) == VOLUME_MIN_DB
    assert _percent_to_db(150) == VOLUME_MAX_DB


def test_set_volume_dispatches_to_coordinator():
    coord = FakeCoordinator(level=10)
    tools = _tools(coord)
    result = asyncio.run(tools["set_volume"](percent=30))
    assert result == {"ok": True, "percent": 30}
    assert coord.calls == [("set", 30)]
    assert coord.get_listening_level() == 30


def test_adjust_volume_relative():
    coord = FakeCoordinator(level=40)
    tools = _tools(coord)
    result = asyncio.run(tools["adjust_volume"](delta_percent=10))
    assert result["percent"] == 50
    assert coord.calls == [("adjust", 10)]


def test_adjust_volume_clamps_high():
    coord = FakeCoordinator(level=95)
    tools = _tools(coord)
    result = asyncio.run(tools["adjust_volume"](delta_percent=20))
    assert result["percent"] == 100


def test_adjust_volume_clamps_low():
    coord = FakeCoordinator(level=5)
    tools = _tools(coord)
    result = asyncio.run(tools["adjust_volume"](delta_percent=-30))
    assert result["percent"] == 0


def test_get_volume_returns_percent():
    coord = FakeCoordinator(level=75)
    tools = _tools(coord)
    result = asyncio.run(tools["get_volume"]())
    assert result == {"percent": 75}


def test_mute_then_unmute_restores_prior_level():
    coord = FakeCoordinator(level=60)
    tools = _tools(coord)
    asyncio.run(tools["mute"]())
    assert coord.get_listening_level() == 0
    result = asyncio.run(tools["unmute"]())
    assert result["percent"] == 60
    assert coord.get_listening_level() == 60


def test_unmute_without_prior_mute_uses_default():
    coord = FakeCoordinator(level=0)
    tools = _tools(coord)
    result = asyncio.run(tools["unmute"]())
    assert result["percent"] == 50
