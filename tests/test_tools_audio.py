# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

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


def test_percent_to_db_round_trips_nonzero_slider_values():
    assert _db_to_percent(_percent_to_db(50)) == 50


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


class _FakeControlResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return self._payload


class _FakeControlClient:
    """Stands in for AsyncControlClient: records (path, body) and replays
    a scripted response (or raises a scripted ControlError)."""

    def __init__(self, status=200, payload=None, error=None):
        self.status = status
        self.payload = payload or {"db": -15.0, "percent": 70,
                                   "pair_leader": "jts.local"}
        self.error = error
        self.seen: list[tuple[str, dict | None]] = []

    async def get(self, path):
        return await self._reply(path, None)

    async def post(self, path, body=None):
        return await self._reply(path, body)

    async def _reply(self, path, body):
        self.seen.append((path, body))
        if self.error is not None:
            raise self.error
        return _FakeControlResponse(self.status, dict(self.payload))


def _arm_follower(monkeypatch, **client_kw):
    """Patch this speaker into an active bonded follower with the control
    client faked. Returns the fake client (inspect .seen)."""
    import jasper.multiroom.config as mcfg
    import jasper.multiroom.effective_role as effective_role
    import jasper.tools.audio as audio_mod

    monkeypatch.setattr(mcfg, "load_config", lambda *a, **k: _follower_cfg())
    monkeypatch.setattr(
        effective_role, "read_effective_role_status", lambda: {},
    )
    fake = _FakeControlClient(**client_kw)
    monkeypatch.setattr(audio_mod, "_control_client", fake)
    return fake


async def test_follower_set_volume_moves_pair_not_local(monkeypatch):
    """On an active bonded follower the local coordinator is INAUDIBLE —
    set_volume must drive the pair through the local control API (whose
    /volume* handlers forward to the leader) and never touch the
    coordinator."""
    fake = _arm_follower(monkeypatch)
    coord = FakeCoordinator(level=10)
    result = await _tools(coord)["set_volume"](30)
    assert result == {"ok": True, "percent": 70}  # the LEADER's answer
    assert coord.calls == []
    assert fake.seen == [("/volume/set", {"percent": 30})]


async def test_follower_adjust_and_get_route_to_pair(monkeypatch):
    fake = _arm_follower(monkeypatch)
    coord = FakeCoordinator(level=10)
    tools = _tools(coord)
    assert (await tools["adjust_volume"](-5))["percent"] == 70
    assert (await tools["get_volume"]())["percent"] == 70
    assert coord.calls == []
    assert fake.seen[0] == ("/volume/adjust", {"delta_percent": -5})
    assert fake.seen[1] == ("/volume", None)  # GET carries no body


async def test_follower_mute_unmute_send_explicit_state(monkeypatch):
    """Voice has distinct mute/unmute INTENTS — the forward must carry an
    explicit {"muted": bool}, never the legacy toggle (a toggle would
    invert a stale intent)."""
    fake = _arm_follower(monkeypatch)
    coord = FakeCoordinator(level=40)
    tools = _tools(coord)
    assert (await tools["mute"]()) == {"ok": True, "muted": True}
    assert (await tools["unmute"]())["percent"] == 70
    assert coord.calls == []
    assert fake.seen[0][1] == {"muted": True}
    assert fake.seen[1][1] == {"muted": False}


async def test_follower_forward_failure_is_a_spoken_error_not_inert_write(
    monkeypatch,
):
    """Leader unreachable → the tool returns an `error` the LLM speaks.
    Falling back to the local coordinator would 'succeed' inaudibly."""
    from jasper.control.client import ControlError

    fake = _arm_follower(
        monkeypatch, error=ControlError("POST", "/volume/set", "no route"),
    )
    coord = FakeCoordinator(level=40)
    result = await _tools(coord)["set_volume"](80)
    assert "error" in result
    assert "pair leader" in result["error"]
    assert coord.calls == []
    assert fake.seen  # the forward was attempted


async def test_follower_leader_reject_is_relayed_not_generic(monkeypatch):
    """jasper-control relays the leader's own error verdict (status+body);
    the tool passes that specific reason to the LLM instead of claiming
    the leader is offline."""
    fake = _arm_follower(
        monkeypatch, status=400,
        payload={"error": "percent must be an integer",
                 "pair_leader": "jts.local"},
    )
    coord = FakeCoordinator(level=40)
    result = await _tools(coord)["set_volume"](80)
    assert result == {"error": "percent must be an integer"}
    assert coord.calls == []
    assert fake.seen


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


async def test_refused_follower_landed_solo_uses_local_coordinator(monkeypatch):
    import jasper.multiroom.config as mcfg
    import jasper.multiroom.effective_role as effective_role
    import jasper.tools.audio as audio_mod

    cfg = _follower_cfg()
    boot_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(mcfg, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(effective_role, "read_current_boot_id", lambda: boot_id)
    monkeypatch.setattr(
        effective_role,
        "read_effective_role_status",
        lambda: {
            "requested_fingerprint": effective_role.grouping_request_fingerprint(cfg),
            "local_sources_allowed": True,
            "boot_id": boot_id,
        },
    )
    client = _FakeControlClient()
    monkeypatch.setattr(audio_mod, "_control_client", client)
    coord = FakeCoordinator(level=10)

    result = await _tools(coord)["set_volume"](30)

    assert result == {"ok": True, "percent": 30}
    assert coord.calls == [("set", 30)]
    assert client.seen == []
