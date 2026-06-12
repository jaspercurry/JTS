from __future__ import annotations

import asyncio
import json

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


# ---------------------------------------------------------------------------
# Bonded-follower pair forward — voice volume on a follower moves the PAIR.
# ---------------------------------------------------------------------------


def _follower_cfg():
    from jasper.multiroom.config import GroupingConfig
    return GroupingConfig(
        enabled=True, role="follower", channel="right", bond_id="bond-1",
        leader_addr="jts.local", buffer_ms=400, codec="flac", error=None,
    )


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _arm_follower(monkeypatch, payload=b'{"db": -15.0, "percent": 70, "pair_leader": "jts.local"}'):
    """Patch this speaker into an active bonded follower with the control
    API call captured. Returns the list of (url, body) requests seen."""
    import jasper.multiroom.config as mcfg
    import jasper.tools.audio as audio_mod

    monkeypatch.setattr(mcfg, "load_config", lambda *a, **k: _follower_cfg())
    seen: list[tuple[str, dict | None]] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data) if req.data else None
        seen.append((req.full_url, body))
        return _FakeResp(payload)

    monkeypatch.setattr(audio_mod, "_pair_urlopen", fake_urlopen)
    return seen


async def test_follower_set_volume_moves_pair_not_local(monkeypatch):
    """On an active bonded follower the local coordinator is INAUDIBLE —
    set_volume must drive the pair through the local control API (whose
    /volume* handlers forward to the leader) and never touch the
    coordinator."""
    seen = _arm_follower(monkeypatch)
    coord = FakeCoordinator(level=10)
    result = await _tools(coord)["set_volume"](30)
    assert result == {"ok": True, "percent": 70}  # the LEADER's answer
    assert coord.calls == []
    url, body = seen[0]
    assert url == "http://127.0.0.1:8780/volume/set"
    assert body == {"percent": 30}


async def test_follower_adjust_and_get_route_to_pair(monkeypatch):
    seen = _arm_follower(monkeypatch)
    coord = FakeCoordinator(level=10)
    tools = _tools(coord)
    assert (await tools["adjust_volume"](-5))["percent"] == 70
    assert (await tools["get_volume"]())["percent"] == 70
    assert coord.calls == []
    assert seen[0][0].endswith("/volume/adjust")
    assert seen[0][1] == {"delta_percent": -5}
    assert seen[1][0].endswith("/volume")
    assert seen[1][1] is None  # GET carries no body


async def test_follower_mute_unmute_send_explicit_state(monkeypatch):
    """Voice has distinct mute/unmute INTENTS — the forward must carry an
    explicit {"muted": bool}, never the legacy toggle (a toggle would
    invert a stale intent)."""
    seen = _arm_follower(monkeypatch)
    coord = FakeCoordinator(level=40)
    tools = _tools(coord)
    assert (await tools["mute"]()) == {"ok": True, "muted": True}
    assert (await tools["unmute"]())["percent"] == 70
    assert coord.calls == []
    assert seen[0][1] == {"muted": True}
    assert seen[1][1] == {"muted": False}


async def test_follower_forward_failure_is_a_spoken_error_not_inert_write(
    monkeypatch,
):
    """Leader unreachable → the tool returns an `error` the LLM speaks.
    Falling back to the local coordinator would 'succeed' inaudibly."""
    import jasper.multiroom.config as mcfg
    import jasper.tools.audio as audio_mod

    monkeypatch.setattr(mcfg, "load_config", lambda *a, **k: _follower_cfg())

    def exploding(req, timeout=None):
        raise OSError("no route to host")

    monkeypatch.setattr(audio_mod, "_pair_urlopen", exploding)
    coord = FakeCoordinator(level=40)
    result = await _tools(coord)["set_volume"](80)
    assert "error" in result
    assert "pair leader" in result["error"]
    assert coord.calls == []


async def test_leader_and_solo_keep_the_local_coordinator(monkeypatch):
    """role=leader (and any non-active-follower shape) never forwards —
    the coordinator IS the pair volume on the leader."""
    import jasper.multiroom.config as mcfg
    from jasper.multiroom.config import GroupingConfig

    monkeypatch.setattr(
        mcfg, "load_config",
        lambda *a, **k: GroupingConfig(
            enabled=True, role="leader", channel="left", bond_id="bond-1",
            leader_addr="", buffer_ms=400, codec="flac", error=None,
        ),
    )
    coord = FakeCoordinator(level=10)
    result = await _tools(coord)["set_volume"](30)
    assert result == {"ok": True, "percent": 30}
    assert ("set", 30) in coord.calls
