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


class FakeCamilla:
    def __init__(self, db: float = -25.0) -> None:
        self._db = db

    async def get_volume_db(self) -> float:
        return self._db

    async def set_volume_db(self, db: float) -> None:
        self._db = db

    async def adjust_volume_db(self, delta: float) -> float:
        self._db += delta
        return self._db


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


def test_set_volume_writes_through():
    cam = FakeCamilla(db=-10.0)
    tools = {f.__name__: f for f in make_audio_tools(cam)}
    result = asyncio.run(tools["set_volume"](percent=30))
    assert result == {"ok": True, "percent": 30}
    assert cam._db == _percent_to_db(30)


def test_adjust_volume_relative():
    cam = FakeCamilla(db=_percent_to_db(40))
    tools = {f.__name__: f for f in make_audio_tools(cam)}
    result = asyncio.run(tools["adjust_volume"](delta_percent=10))
    assert result["percent"] == 50


def test_adjust_volume_clamps_high():
    cam = FakeCamilla(db=_percent_to_db(95))
    tools = {f.__name__: f for f in make_audio_tools(cam)}
    result = asyncio.run(tools["adjust_volume"](delta_percent=20))
    assert result["percent"] == 100


def test_adjust_volume_clamps_low():
    cam = FakeCamilla(db=_percent_to_db(5))
    tools = {f.__name__: f for f in make_audio_tools(cam)}
    result = asyncio.run(tools["adjust_volume"](delta_percent=-30))
    assert result["percent"] == 0


def test_get_volume_returns_percent():
    cam = FakeCamilla(db=_percent_to_db(75))
    tools = {f.__name__: f for f in make_audio_tools(cam)}
    result = asyncio.run(tools["get_volume"]())
    assert result == {"percent": 75}


def test_mute_then_unmute_restores_prior_level():
    cam = FakeCamilla(db=_percent_to_db(60))
    tools = {f.__name__: f for f in make_audio_tools(cam)}
    asyncio.run(tools["mute"]())
    assert _db_to_percent(cam._db) == 0
    result = asyncio.run(tools["unmute"]())
    assert result["percent"] == 60
    assert _db_to_percent(cam._db) == 60


def test_unmute_without_prior_mute_uses_default():
    cam = FakeCamilla(db=_percent_to_db(0))
    tools = {f.__name__: f for f in make_audio_tools(cam)}
    result = asyncio.run(tools["unmute"]())
    assert result["percent"] == 50
