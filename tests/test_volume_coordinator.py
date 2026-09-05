# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.volume_coordinator.

The coordinator is the product's only writer in front of
``CamillaController.set_volume_db``, so the mute, unmute-ordering, guard and
duck-arbitration pins here sit on AGENTS.md non-negotiable 1.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from tests._async_wait import wait_signalled

from jasper import bluealsa_probe
from jasper import spotify_router as spotify_router_mod
from jasper import volume_coordinator as vc_mod
from jasper.accounts import Account
from jasper.camilla import CamillaUnavailable
from jasper.spotify_router import AccountClient, Router
from jasper.volume_coordinator import (
    BT_VOLUME_MAX,
    ECHO_WINDOW_SEC,
    Source,
    VolumeCoordinator,
    bt_volume_to_listening_level,
    listening_level_to_bt_volume,
    listening_level_to_spotify_percent,
    spotify_percent_to_listening_level,
)
from jasper.volume_diagnostics import (
    PUSH_NO_ACTIVE_DEVICE,
    PUSH_OK,
    PUSH_WRITE_FAILED,
    read_diagnostics,
)
from jasper.volume_owner import ClaimKind
from jasper.volume_persistence import VolumePersistence, percent_to_db


@pytest.fixture(autouse=True)
def _reset_bluealsa_probe_state():
    bluealsa_probe._reset_for_tests()
    yield
    bluealsa_probe._reset_for_tests()


# ---------- mapping helpers -------------------------------------------------


# AirPlay has no mapping helper here: shairport's volume hook owns that
# scale and reaches the coordinator in percent (ADR-0206). Its endpoints are
# pinned against AIRPLAY_DB_MIN/MAX in tests/test_airplay_volume_hook.py.


@pytest.mark.parametrize("level", [0, 50, 100])
def test_spotify_round_trip(level):
    pct = listening_level_to_spotify_percent(level)
    assert spotify_percent_to_listening_level(pct) == level


@pytest.mark.parametrize("level", [0, 25, 50, 75, 100])
def test_bt_round_trip(level):
    vol = listening_level_to_bt_volume(level)
    assert 0 <= vol <= BT_VOLUME_MAX
    # ±1pp slack for the percent↔127 conversion at non-multiples
    assert abs(bt_volume_to_listening_level(vol) - level) <= 1


def test_clamping_below_zero_and_above_100():
    assert listening_level_to_bt_volume(-10) == 0
    assert listening_level_to_bt_volume(150) == BT_VOLUME_MAX


# ---------- doubles and builders -------------------------------------------


class _FakeCamilla:
    def __init__(self, db: float = 0.0) -> None:
        self._db = db
        self.muted = False
        self.set_calls: list[float] = []
        self.mute_calls: list[bool] = []
        self.events: list[tuple[str, float | bool]] = []
        self.get_calls: int = 0
        # When True, every best_effort call is a no-op (writes return
        # False, reads return None) to simulate a camilla restart blip.
        # Non-best_effort calls raise CamillaUnavailable.
        self.unavailable = False

    async def get_volume_db(self, *, best_effort: bool = False) -> float | None:
        self.get_calls += 1
        if self.unavailable:
            if best_effort:
                return None
            raise CamillaUnavailable("test fake offline")
        return self._db

    async def get_volume_and_mute(
        self, *, best_effort: bool = False,
    ) -> tuple[float, bool] | None:
        self.get_calls += 1
        if self.unavailable:
            if best_effort:
                return None
            raise CamillaUnavailable("test fake offline")
        return self._db, self.muted

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        if self.unavailable:
            if best_effort:
                return False
            raise CamillaUnavailable("test fake offline")
        self._db = db
        self.set_calls.append(db)
        self.events.append(("volume", db))
        return True

    async def set_main_mute(
        self, muted: bool, *, best_effort: bool = False,
    ) -> bool:
        if self.unavailable:
            if best_effort:
                return False
            raise CamillaUnavailable("test fake offline")
        self.muted = bool(muted)
        self.mute_calls.append(bool(muted))
        self.events.append(("mute", bool(muted)))
        return True


class _FakeBackend:
    def __init__(
        self,
        active: dict[str, bool] | None = None,
        selected: str | None = None,
    ) -> None:
        self._active = active or {}
        self._selected = selected

    async def active_renderers(self) -> dict[str, bool]:
        return dict(self._active)

    async def selected_source(self) -> str | None:
        return self._selected


class _RecordingCoordinator(VolumeCoordinator):
    """Records source-side dispatch instead of invoking busctl / HTTP.

    Mirrors production semantics: idle/AirPlay use camilla; Spotify/BT are
    push-mode.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.airplay_writes: list[int] = []
        self.spotify_writes: list[int] = []
        self.bt_writes: list[int] = []
        self.camilla_writes: list[int] = []

    async def _set_airplay(self, level: int) -> bool:
        self.airplay_writes.append(level)
        return await self._set_camilla(level)

    async def _set_spotify(self, level: int) -> bool:
        self.spotify_writes.append(level)
        self._stamp_outbound(Source.SPOTIFY, level)
        return True

    async def _set_bluetooth(self, level: int) -> bool:
        self.bt_writes.append(level)
        self._stamp_outbound(Source.BLUETOOTH, level)
        return True

    async def _set_camilla(self, level: int) -> bool:
        ok = await super()._set_camilla(level)
        self.camilla_writes.append(level)
        return ok


class _BlockingMuteCoordinator(_RecordingCoordinator):
    """Pause only the Spotify 0% write to expose the persisted pre-push gap."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.mute_push_started = asyncio.Event()
        self.release_mute_push = asyncio.Event()

    async def _set_spotify(self, level: int) -> bool:
        self.spotify_writes.append(level)
        if level == 0:
            self.mute_push_started.set()
            await self.release_mute_push.wait()
        self._stamp_outbound(Source.SPOTIFY, level)
        return True


def _build(
    cls,
    tmp_path,
    *,
    active: dict[str, bool] | None = None,
    selected: str | None = None,
    db: float = 0.0,
    level: int | None = None,
    mark_user_change: bool = False,
    **kwargs,
):
    """Coordinator over a fresh on-disk record; returns (coord, cam, store).

    ``level`` seeds both the in-memory canonical level and the persisted one,
    which is what a coordinator that has already served a set looks like.
    """
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=db)
    coord = cls(
        camilla=cam,
        persistence=persistence,
        backend=_FakeBackend(active=active, selected=selected),
        **kwargs,
    )
    if level is not None:
        coord._level = level
        persistence.save_listening_level(
            level, mark_user_change=mark_user_change,
        )
    return coord, cam, persistence


def _coord(tmp_path, **kwargs):
    """Recording coordinator, for tests about what dispatch chose."""
    kwargs.setdefault("handoff_settle_sec", 0.0)
    return _build(_RecordingCoordinator, tmp_path, **kwargs)


def _real_coord(tmp_path, **kwargs):
    """Production coordinator, for tests whose subject is its own dispatch."""
    return _build(VolumeCoordinator, tmp_path, **kwargs)


def _assert_persisted(
    persistence,
    *,
    level: int | None = None,
    db: float | None = None,
    db_abs: float | None = None,
) -> None:
    """Read back the shared record and assert the fields named."""
    record = persistence.load()
    assert record is not None
    if level is not None:
        assert record.listening_level == level
    if db is not None:
        assert record.main_volume_db == pytest.approx(db, abs=db_abs)


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == vc_mod.__name__ and record.levelno >= logging.WARNING
    ]


def _event_fields(caplog, event: str) -> dict[str, str]:
    """The ONE logfmt record for `event`, as its k=v field map."""
    matches = [
        record.getMessage()
        for record in caplog.records
        if record.name == vc_mod.__name__
        and record.getMessage().startswith(f"event={event} ")
    ]
    assert len(matches) == 1, matches
    return dict(
        token.split("=", 1) for token in matches[0].split() if "=" in token
    )


async def test_aclose_is_safe_without_owned_observer_tasks(tmp_path):
    coord, _, _ = _coord(tmp_path, active={})

    await coord.aclose()


# ---------- outbound dispatch ----------------------------------------------


async def test_set_volume_idle_writes_camilla(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={})
    await coord.set_listening_level(70)
    assert coord.camilla_writes == [70]
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(70))
    assert cam.mute_calls[-1] is False


async def test_set_volume_zero_hard_mutes_camilla_master(tmp_path):
    """0% content volume is a real mute, not just the dB curve bottom."""
    coord, cam, persistence = _coord(tmp_path, active={})

    await coord.set_listening_level(0)

    assert coord.camilla_writes == [0]
    assert cam.mute_calls[-1] is True
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    _assert_persisted(persistence, level=0, db=percent_to_db(0))


async def test_set_volume_nonzero_clears_mute_after_volume_write(tmp_path):
    """Unmute order is volume first, then main_mute=false, so there is
    no full-scale transient while returning from a 0% content mute."""
    coord, cam, _ = _coord(tmp_path, active={}, db=-50.0)
    cam.muted = True

    await coord.set_listening_level(75)

    assert cam.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(75))),
        ("mute", False),
    ]


async def test_set_volume_airplay_active_routes_to_camilla(tmp_path):
    """AirPlay is camilla-as-master: remote/voice/HTTP changes must be
    audible even though modern AirPlay 2 sender slider reflection via
    shairport-sync is unavailable."""
    coord, _, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(50)
    assert coord.airplay_writes == [50]
    assert coord.camilla_writes == [50]


async def test_manual_selected_source_overrides_raw_renderer_probe(tmp_path):
    """Source selection gates what the speaker actually passes, so
    volume dispatch follows mux's manual selection over raw activity."""
    coord, _, _ = _coord(
        tmp_path, active={"aplactive": True}, selected="spotify",
    )

    await coord.set_listening_level(55)

    assert coord.spotify_writes == [55]
    assert coord.airplay_writes == []


async def test_set_volume_spotify_active_routes_to_spotify(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True}, db=-25.0)
    await coord.set_listening_level(40)
    assert coord.spotify_writes == [40]
    assert cam.set_calls == []  # Spotify is push-mode; camilla untouched


async def test_push_mode_zero_sets_final_mute_after_source_push(tmp_path):
    coord, cam, persistence = _coord(tmp_path, active={"spotactive": True})

    await coord.set_listening_level(0)

    assert coord.spotify_writes == [0]
    assert cam.mute_calls[-1] is True
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    _assert_persisted(persistence, db=percent_to_db(0))


async def test_push_mode_nonzero_clears_stale_final_mute(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True}, db=-50.0)
    cam.muted = True

    await coord.set_listening_level(75)

    assert coord.spotify_writes == [75]
    assert cam.events[-2:] == [
        ("volume", pytest.approx(0.0)),
        ("mute", False),
    ]


async def test_set_volume_spotify_failure_updates_camilla_guard(tmp_path):
    """If the active push source cannot accept volume, normal user
    volume changes still keep the audible path guarded by Camilla."""
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True})

    async def fail_spotify(_level: int) -> bool:
        return False

    coord._set_spotify = fail_spotify

    await coord.set_listening_level(25)

    assert cam.set_calls[-1] == pytest.approx(percent_to_db(25))


# ---------- _set_spotify's own device walk (every other test above stubs it) --


def _spotify_account(*, devices_fn, volume_fn=None) -> AccountClient:
    sp = MagicMock()
    sp.devices = devices_fn
    if volume_fn is not None:
        sp.volume = volume_fn
    return AccountClient(account=Account(name="primary"), sp=sp)


@pytest.mark.parametrize(
    ("case", "expect_ok", "expect_reason"),
    [
        ("hung", False, PUSH_NO_ACTIVE_DEVICE),
        ("no_match", False, PUSH_NO_ACTIVE_DEVICE),
        ("write_raises", False, PUSH_WRITE_FAILED),
        ("ok", True, PUSH_OK),
    ],
)
async def test_set_spotify_pins_diagnostic_by_scenario(
    tmp_path, monkeypatch, case, expect_ok, expect_reason,
):
    """`_set_spotify` walks Router.devices_named itself (unlike every other
    test in this file, which stubs `_set_spotify` outright) — so this is the
    only place its own diagnostic/stamp outcomes get pinned."""
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    monkeypatch.setattr(spotify_router_mod, "DEVICES_TIMEOUT_SEC", 0.05)
    release = threading.Event()
    volume_calls: list[int] = []

    if case == "hung":
        def _hang():
            release.wait(timeout=10.0)
            return {"devices": []}
        ac = _spotify_account(devices_fn=_hang)
    elif case == "no_match":
        ac = _spotify_account(
            devices_fn=lambda: {"devices": [{"name": "Phone", "id": "p1"}]},
        )
    elif case == "write_raises":
        def _raise(pct, device_id):
            volume_calls.append(pct)
            raise RuntimeError("simulated write failure")
        ac = _spotify_account(
            devices_fn=lambda: {"devices": [{"name": "JTS", "id": "jts-1"}]},
            volume_fn=_raise,
        )
    else:
        def _ok(pct, device_id):
            volume_calls.append(pct)
        ac = _spotify_account(
            devices_fn=lambda: {"devices": [{"name": "JTS", "id": "jts-1"}]},
            volume_fn=_ok,
        )

    router = Router(clients={"primary": ac}, default_name="primary")
    coord, _, _ = _real_coord(
        tmp_path, active={}, spotify_router=router, spotify_device_name="JTS",
    )
    try:
        result = await asyncio.wait_for(coord._set_spotify(55), timeout=5.0)
    finally:
        release.set()

    assert result is expect_ok
    push_result = read_diagnostics(str(diag_path))["last_source_push_result"]
    assert push_result["ok"] is expect_ok
    assert push_result["reason"] == expect_reason
    if case == "ok":
        assert coord._last_outbound[Source.SPOTIFY].level == 55
        assert volume_calls == [listening_level_to_spotify_percent(55)]
    else:
        assert Source.SPOTIFY not in coord._last_outbound


def _assert_push_failure_outcome(
    caplog,
    diag_path,
    *,
    source: Source,
    level: int,
    reason: str,
    context: str,
    guard_confirmed: bool,
    unconfirmed_warning: str,
) -> None:
    """Both halves of the guard contract, shared by both entry points."""
    diagnostics = read_diagnostics(str(diag_path))
    if not guard_confirmed:
        # An unconfirmed guard reaches diagnostics not at all, so the operator
        # wording is the only surface there is to pin.
        assert _warnings(caplog) == [unconfirmed_warning]
        assert "push_guard" not in diagnostics
        return
    assert len(_warnings(caplog)) == 1
    push_guard = dict(diagnostics["push_guard"])
    assert isinstance(push_guard.pop("updated_at"), str)
    assert push_guard == {
        "active": True,
        "source": source.value,
        "level": level,
        "guard_db": pytest.approx(round(percent_to_db(level), 2)),
        "previous_db": -7.5,
        "reason": reason,
        "context": context,
    }


def _stub_failed_push(monkeypatch, coord, setter_name: str, guard_confirmed: bool):
    """Refuse the source push; record what the camilla guard was asked for."""
    guard_calls: list[tuple[float, str, bool]] = []

    async def fail_push(_level: int) -> bool:
        return False

    async def guard_camilla(db: float, *, context: str, persist: bool) -> bool:
        guard_calls.append((db, context, persist))
        return guard_confirmed

    monkeypatch.setattr(coord, setter_name, fail_push)
    monkeypatch.setattr(coord, "_set_camilla_db", guard_camilla)
    return guard_calls


@pytest.mark.parametrize(
    ("source", "active_key", "setter_name", "context"),
    [
        (
            Source.SPOTIFY,
            "spotactive",
            "_set_spotify",
            "dispatch_spotify_degraded",
        ),
        (
            Source.BLUETOOTH,
            "btactive",
            "_set_bluetooth",
            "dispatch_bluetooth_degraded",
        ),
    ],
)
@pytest.mark.parametrize("guard_confirmed", [True, False])
async def test_push_dispatch_failure_guard_preserves_diagnostics_and_warning(
    tmp_path,
    monkeypatch,
    caplog,
    source: Source,
    active_key: str,
    setter_name: str,
    context: str,
    guard_confirmed: bool,
):
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    coord, _, persistence = _coord(tmp_path, active={active_key: True}, level=70)
    persistence.save_now(-7.5)
    guard_calls = _stub_failed_push(
        monkeypatch, coord, setter_name, guard_confirmed,
    )
    level = 25
    guard_db = percent_to_db(level)

    with caplog.at_level(logging.WARNING, logger=vc_mod.__name__):
        await coord.set_listening_level(level)

    assert guard_calls == [(pytest.approx(guard_db), context, True)]
    _assert_push_failure_outcome(
        caplog,
        diag_path,
        source=source,
        level=level,
        reason="push_write_failed",
        context=context,
        guard_confirmed=guard_confirmed,
        unconfirmed_warning=(
            f"{source.value} volume dispatch failed and camilla guard could "
            f"not be confirmed for {guard_db:.1f} dB"
        ),
    )


async def test_set_volume_bluetooth_active_routes_to_bt(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={"btactive": True}, db=-25.0)
    await coord.set_listening_level(60)
    assert coord.bt_writes == [60]
    assert cam.set_calls == []  # BT is push-mode; camilla untouched


# Each row: the renderer set before the set, the fader dB it starts at, the
# level set on it, the renderer set mux flips to (None = unchanged), the
# transition reported, then what the transition itself wrote to camilla
# ("carrier" None = camilla was never written at all) and what each source
# recorder holds at the end.
_TRANSITION_CARRIERS = [
    dict(
        id="idle_to_push", before={"spotactive": True}, db=-25.0, level=50,
        after=None, prev=Source.IDLE, current=Source.SPOTIFY,
        writes=[0.0], carrier=0.0, spotify=[50, 50], bt=[], airplay=[],
    ),
    dict(
        id="camilla_master_to_push", before={"aplactive": True}, db=0.0,
        level=60, after={"spotactive": True},
        prev=Source.AIRPLAY, current=Source.SPOTIFY,
        writes=[0.0], carrier=0.0, spotify=[60], bt=[], airplay=[60],
    ),
    dict(
        id="push_to_idle", before={}, db=0.0, level=60,
        after=None, prev=Source.SPOTIFY, current=Source.IDLE,
        writes=[], carrier=percent_to_db(60), spotify=[], bt=[], airplay=[],
    ),
    dict(
        id="push_to_camilla_master", before={"spotactive": True}, db=0.0,
        level=50, after={"aplactive": True},
        prev=Source.SPOTIFY, current=Source.AIRPLAY,
        writes=[percent_to_db(50)], carrier=percent_to_db(50),
        spotify=[50], bt=[], airplay=[],
    ),
    dict(
        id="idle_to_camilla_master", before={}, db=0.0, level=40,
        after={"aplactive": True}, prev=Source.IDLE, current=Source.AIRPLAY,
        writes=[], carrier=percent_to_db(40), spotify=[], bt=[], airplay=[],
    ),
    dict(
        id="push_to_push", before={"spotactive": True}, db=0.0, level=55,
        after={"btactive": True},
        prev=Source.SPOTIFY, current=Source.BLUETOOTH,
        writes=[], carrier=None, spotify=[55], bt=[55], airplay=[],
    ),
]


@pytest.mark.parametrize(
    "case", _TRANSITION_CARRIERS, ids=lambda case: case["id"],
)
async def test_which_attenuator_carries_the_level_across_a_transition(
    tmp_path, case,
):
    """A camilla-master lane keeps percent_to_db(level) on the fader; a
    push-mode lane pins the fader to 0 dB and puts the level on the source's
    own slider. The transition writes only what changing carriers needs.
    """
    coord, cam, _ = _coord(tmp_path, active=case["before"], db=case["db"])
    await coord.set_listening_level(case["level"])
    before = len(cam.set_calls)
    if case["after"] is not None:
        coord._backend = _FakeBackend(active=case["after"])

    await coord.apply_active_source_transition(case["prev"], case["current"])

    assert cam.set_calls[before:] == [
        pytest.approx(db) for db in case["writes"]
    ]
    if case["carrier"] is None:
        assert cam.set_calls == []
    else:
        assert cam.set_calls[-1] == pytest.approx(case["carrier"])
    assert coord.spotify_writes == case["spotify"]
    assert coord.bt_writes == case["bt"]
    assert coord.airplay_writes == case["airplay"]


async def test_transition_suppressed_during_voice_session(tmp_path):
    """note_voice_session(True) gates apply_active_source_transition
    so the ducker's additive math isn't corrupted by absolute writes."""
    coord, cam, _ = _coord(tmp_path, active={})
    coord.note_voice_session(True)
    initial_calls = list(cam.set_calls)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)
    assert cam.set_calls == initial_calls

    coord.note_voice_session(False)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)

    assert coord.spotify_writes[-1] == coord.get_listening_level()
    assert cam.muted is False


@pytest.mark.parametrize(
    ("active", "level"),
    [
        pytest.param(
            {"aplactive": True, "spotactive": True, "btactive": True},
            50,
            id="over_spotify_and_bt",
        ),
        pytest.param(
            {"aplactive": True, "usbsinkactive": True}, 55, id="over_usbsink",
        ),
    ],
)
async def test_airplay_outranks_every_other_active_renderer(
    tmp_path, active, level,
):
    """Several renderers can report active during a mux transition window.
    The chain is airplay > spotify > bluetooth > usbsink, matching mux's
    first-source-defined-wins behaviour: a phone-controlled AirPlay session
    is not silently overridden by a Mac plugged into the USB port."""
    coord, _, _ = _coord(tmp_path, active=active)

    await coord.set_listening_level(level)

    assert coord.airplay_writes == [level]
    assert coord.camilla_writes == [level]
    assert coord.spotify_writes == []
    assert coord.bt_writes == []


async def test_adjust_volume(tmp_path):
    """Push-mode adjust path: each set/adjust pushes a fresh value
    to the source's slider."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(50)
    await coord.adjust_listening_level(15)
    assert coord.spotify_writes == [50, 65]


async def test_adjust_clamps_to_0_and_100(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(95)
    await coord.adjust_listening_level(20)
    assert coord.spotify_writes[-1] == 100
    await coord.adjust_listening_level(-200)
    assert coord.spotify_writes[-1] == 0


async def test_mute_then_unmute(tmp_path):
    coord, cam, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(70)
    saved = await coord.mute()
    assert saved == 70
    assert coord.spotify_writes[-1] == 0  # silence
    assert cam.mute_calls[-1] is True
    assert coord.is_muted()
    # The canonical level remains the restore target; every external surface
    # consumes the shared effective projection and therefore renders 0%.
    assert coord.get_listening_level() == 70
    assert coord.get_volume_state().effective_percent == 0
    assert coord.get_volume_state().restore_percent == 70
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 70
    assert record.pre_mute_level == 70
    restored = await coord.unmute()
    assert restored == 70
    assert coord.spotify_writes[-1] == 70
    assert cam.mute_calls[-1] is False
    assert not coord.is_muted()


async def test_push_observer_preserves_cross_process_mute_restore_level(tmp_path):
    """A remote mute's renderer-side 0% echo cannot overwrite its restore level."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    control_coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
    )
    observer_coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
    )

    await control_coord.set_listening_level(60)
    await control_coord.mute()

    accepted = await observer_coord.observe_source_volume(Source.SPOTIFY, 0)

    assert accepted is True
    state = observer_coord.get_volume_state()
    assert state.effective_percent == 0
    assert state.restore_percent == 60
    assert persistence.load().listening_level == 60
    assert persistence.load().pre_mute_level == 60

    # A subsequent, explicit non-zero source-side change ends the temporary
    # mute and becomes the new canonical level.
    await observer_coord.observe_source_volume(Source.SPOTIFY, 65)
    state = observer_coord.get_volume_state()
    assert state.effective_percent == 65
    assert state.restore_percent is None


async def test_push_observer_rejects_stale_nonzero_while_mute_push_pending(
    tmp_path,
):
    """A pre-push renderer reading cannot cancel another process's mute."""
    state_path = str(tmp_path / "speaker_volume.json")
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    control_coord = _BlockingMuteCoordinator(
        camilla=cam,
        persistence=VolumePersistence(state_path),
        backend=backend,
    )
    observer_coord = _RecordingCoordinator(
        camilla=cam,
        persistence=VolumePersistence(state_path),
        backend=backend,
    )
    await control_coord.set_listening_level(60)

    mute_task = asyncio.create_task(control_coord.mute())
    await wait_signalled(
        control_coord.mute_push_started,
        "mute push began",
        producer=mute_task,
    )

    # mute() has persisted its latch and asserted Camilla main_mute, but the
    # slow source surface still exposes its old 60%. A second process waits for
    # the in-flight intent to finish rather than interpreting half-applied
    # physical state.
    observation = asyncio.create_task(
        observer_coord.observe_source_volume(Source.SPOTIFY, 60),
    )
    await asyncio.sleep(0)
    assert observation.done() is False

    control_coord.release_mute_push.set()
    await mute_task
    accepted = await observation
    assert accepted is False
    pending = observer_coord.get_volume_state()
    assert pending.effective_percent == 0
    assert pending.restore_percent == 60
    assert pending.mute_token is not None

    # Seeing zero for this exact mute token opens the barrier. A later nonzero
    # observation is now an unambiguous source-side user edit.
    assert await observer_coord.observe_source_volume(Source.SPOTIFY, 0) is True
    assert await observer_coord.observe_source_volume(Source.SPOTIFY, 65) is True
    settled = observer_coord.get_volume_state()
    assert settled.effective_percent == 65
    assert settled.restore_percent is None
    assert settled.mute_token is None


async def test_push_observer_requires_zero_for_each_new_mute_token(tmp_path):
    """Confirmation from an older mute cannot authorize a newer transition."""
    state_path = str(tmp_path / "speaker_volume.json")
    writer = VolumePersistence(state_path)
    observer = _RecordingCoordinator(
        camilla=_FakeCamilla(db=0.0),
        persistence=VolumePersistence(state_path),
        backend=_FakeBackend(active={"spotactive": True}),
    )
    writer.save_listening_level(60)
    writer.save_mute_state(60, "mute-a")
    assert await observer.observe_source_volume(Source.SPOTIFY, 0) is True

    # An unmute + a second mute that both land between observer polls: the
    # remembered token-A confirmation must not leak into token B.
    writer.save_mute_state(None, None)
    writer.save_listening_level(60)
    writer.save_mute_state(60, "mute-b")

    assert await observer.observe_source_volume(Source.SPOTIFY, 60) is False
    state = observer.get_volume_state()
    assert state.effective_percent == 0
    assert state.restore_percent == 60
    assert state.mute_token == "mute-b"


async def test_unmute_without_prior_mute_uses_fallback(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    restored = await coord.unmute(fallback_level=50)
    assert restored == 50


# ---------- echo prevention ------------------------------------------------


# Echo-prevention tests use SPOTIFY as a representative push-mode source.


@pytest.mark.parametrize("observed", [60, 30])
async def test_observe_within_echo_window_ignored(tmp_path, observed):
    """A poll can briefly see either our own value echoed back or stale
    source state right after our write, especially during source handoff;
    ignore the whole echo window regardless of what it reports."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)

    await coord.observe_source_volume(Source.SPOTIFY, observed)

    assert coord.get_listening_level() == 60
    assert coord.spotify_writes == [60]


async def test_observe_outside_echo_window_becomes_canonical(tmp_path, monkeypatch):
    coord, _, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    # Fast-forward past the echo window without sleeping.
    fake_now = time.monotonic() + ECHO_WINDOW_SEC + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)

    await coord.observe_source_volume(Source.SPOTIFY, 40)

    assert coord.get_listening_level() == 40
    _assert_persisted(persistence, level=40)
    # An observation must NOT trigger an outbound dispatch (no echo).
    assert coord.spotify_writes == [60]


@pytest.mark.parametrize("seeded_level", [50, 100])
async def test_observe_spotify_clears_degraded_guard(tmp_path, seeded_level):
    """A source-side Spotify slider move proves the source volume surface is
    carrying user intent — including when it lands on the level JTS already
    remembers. Clear any degraded-safe Camilla guard so the path returns to
    normal push-mode loudness."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0, level=seeded_level,
    )
    await coord._set_camilla_db(
        -25.0, context="test_degraded_guard", persist=True,
    )

    await coord.observe_source_volume(Source.SPOTIFY, 100)

    assert coord.get_listening_level() == 100
    assert cam.set_calls[-1] == pytest.approx(0.0)
    _assert_persisted(persistence, level=100, db=0.0)


async def test_equal_spotify_observation_publishes_only_when_guard_changes(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, _, persistence = _real_coord(
        tmp_path,
        active={"spotactive": True},
        db=-25.0,
        level=100,
        volume_context_publisher=publish,
    )
    persistence.save_now(-25.0)

    await coord.observe_source_volume(Source.SPOTIFY, 100)
    assert len(published) == 1
    assert published[0].downstream_db == pytest.approx(0.0)

    published.clear()
    await coord.observe_source_volume(Source.SPOTIFY, 100)
    assert published == []


async def test_observe_spotify_clear_deferred_during_duck_keeps_guard(
    tmp_path, monkeypatch,
):
    """A push confirmation during an active duck is not a real carrier
    clear. Keep the guard persisted so the observer can retry later."""
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-13.0, level=90,
    )
    persistence.save_now(-13.0)

    async def probe():
        return True

    coord._duck_active_probe = probe

    await coord.observe_source_volume(Source.SPOTIFY, 90)

    assert cam.set_calls == []
    _assert_persisted(persistence, level=90, db=-13.0)
    diag = read_diagnostics(str(diag_path))
    assert diag["last_clear_event"]["ok"] is False
    assert diag["last_clear_event"]["reason"] == "clear_deferred_duck_active"
    assert diag.get("push_guard", {}).get("active") is not False


async def test_observe_spotify_repairs_live_guard_after_false_clear(tmp_path):
    """Recover from the legacy split-brain: persistence claimed the push
    guard was clear, but live Camilla was still attenuating the path."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-13.0, level=90,
    )
    persistence.save_now(0.0)

    async def probe():
        return False

    coord._duck_active_probe = probe

    await coord.observe_source_volume(Source.SPOTIFY, 90)

    assert cam.set_calls[-1] == pytest.approx(0.0)
    _assert_persisted(persistence, level=90, db=0.0)


async def test_successful_push_dispatch_clears_degraded_guard(
    tmp_path, monkeypatch,
):
    """If a later outbound push succeeds, Camilla should stop carrying
    the degraded fallback attenuation."""
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0, level=50,
    )
    await coord._set_camilla_db(
        -25.0, context="test_degraded_guard", persist=True,
    )

    await coord.set_listening_level(50)

    assert coord.spotify_writes == [50]
    assert cam.set_calls[-1] == pytest.approx(0.0)
    _assert_persisted(persistence, level=50, db=0.0)
    diag = read_diagnostics(str(diag_path))
    assert diag["push_guard"]["active"] is False
    assert diag["last_clear_event"]["source"] == "spotify"
    assert diag["last_clear_event"]["previous_db"] == pytest.approx(-25.0)
    assert diag["last_clear_event"]["reason"] == "push_confirmed"


async def test_observe_respects_recent_cross_process_write(tmp_path):
    """Hardware knobs hit jasper-control, which has a separate
    coordinator and no shared outbound stamp. A stale observer poll
    should not undo the freshly persisted knob level."""
    coord, _, persistence = _coord(
        tmp_path, active={"spotactive": True}, level=70,
    )

    # jasper-control in another process handling a knob twist.
    persistence.save_listening_level(80)

    assert coord._is_recent_cross_process_write(70)
    await coord.observe_source_volume(Source.SPOTIFY, 70)

    assert coord.get_listening_level() == 80
    _assert_persisted(persistence, level=80)


async def test_observe_revalidates_active_source_at_mutation_boundary(tmp_path):
    """A queued observation cannot land after mux has switched lanes."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True}, level=60)
    active_sources = iter([Source.SPOTIFY, Source.BLUETOOTH])

    async def changing_active_source():
        return next(active_sources)

    coord._active_source = changing_active_source

    assert await coord.observe_source_volume(Source.SPOTIFY, 40) is False
    assert coord.get_volume_state().effective_percent == 60


# ---------- initialize / boot regression ----------------------------------


async def test_initialize_first_boot_uses_default(tmp_path):
    coord, _, persistence = _coord(tmp_path, active={})
    target, reason = await coord.initialize(first_boot_default_pct=42)
    assert target == 42
    assert "first-boot" in reason
    _assert_persisted(persistence, level=42)


async def test_initialize_does_not_bump_last_used_at(tmp_path):
    """Boot-time restore must NOT update last_used_at — otherwise
    every restart resets the idle-reset clock and yesterday's
    bedtime 90% never gets clamped."""
    coord, _, persistence = _coord(tmp_path, active={})
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    persistence._current_main_volume_db = -25.0
    persistence._current_listening_level = 90
    persistence._current_last_used_at = old_ts
    persistence._write_full()

    await coord.initialize(
        stale_after_sec=60.0,
        safe_low_pct=20, safe_high_pct=70,
        first_boot_default_pct=50,
    )

    rec = persistence.load()
    assert rec is not None
    assert rec.last_used_at is not None
    # 1 s tolerance for the persistence round-trip.
    assert abs((rec.last_used_at - old_ts).total_seconds()) < 1.0


async def test_user_change_bumps_last_used_at(tmp_path):
    coord, _, persistence = _coord(tmp_path, active={})
    await coord.set_listening_level(45)
    rec = persistence.load()
    assert rec is not None
    assert rec.last_used_at is not None
    age = (datetime.now(timezone.utc) - rec.last_used_at).total_seconds()
    assert 0 <= age < 5


# ---------- AirPlay camilla-master dispatch --------------------------------


async def test_set_airplay_delegates_to_camilla_without_subprocess(
    tmp_path, monkeypatch,
):
    """Real _set_airplay path: use CamillaDSP as the reliable audible
    AirPlay volume surface, not shairport-sync DACP/DBus."""
    coord, cam, _ = _real_coord(tmp_path, active={})

    async def fail_spawn(*args, **kwargs):
        raise AssertionError("AirPlay should not spawn a control subprocess")

    monkeypatch.setattr(vc_mod.asyncio, "create_subprocess_exec", fail_spawn)

    await coord._set_airplay(75)

    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(75))
    assert Source.AIRPLAY not in coord._last_outbound


async def test_observe_airplay_moves_the_camilla_master(tmp_path):
    """The sender's slider is an inbound control surface (ADR-0206): its
    observation becomes the canonical level and, because AirPlay is
    camilla-as-master, lands on CamillaDSP's ramped fader."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={"aplactive": True}, level=70,
    )

    accepted = await coord.observe_source_volume(Source.AIRPLAY, 30)

    assert accepted is True
    assert coord.get_listening_level() == 30
    _assert_persisted(persistence, level=30)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(
        percent_to_db(30)
    )


@pytest.mark.parametrize(
    ("initial", "expect_accepted"),
    [(False, True), (True, False)],
)
async def test_observe_airplay_while_muted_turns_on_initial(
    tmp_path, initial, expect_accepted,
):
    """`observation_initial` is the whole session-start guard (ADR-0206).

    A plain observation is the user reaching for the volume, so it clears the
    mute. The one observation shairport pushes when a sender connects is
    marked initial, and that one must leave a latched mute alone — otherwise
    connecting a Mac unmutes a speaker the owner silenced.
    """
    coord, _, _ = _real_coord(tmp_path, active={"aplactive": True}, level=70)
    await coord.set_muted(True)
    assert coord.get_volume_state().pre_mute_level is not None

    accepted = await coord.observe_source_volume(
        Source.AIRPLAY, 40, initial=initial,
    )

    assert accepted is expect_accepted
    state = coord.get_volume_state()
    if expect_accepted:
        assert state.pre_mute_level is None
        assert state.listening_level == 40
    else:
        assert state.pre_mute_level is not None


@pytest.mark.parametrize(
    ("active", "observed_source"),
    [
        pytest.param({"spotactive": True}, Source.AIRPLAY, id="airplay_vs_spotify"),
        pytest.param({"aplactive": True}, Source.USBSINK, id="usbsink_vs_airplay"),
    ],
)
async def test_observe_inactive_source_is_ignored(
    tmp_path, active, observed_source,
):
    """Stale readings from a non-current renderer must not steal the
    canonical level from the active source."""
    coord, _, _ = _real_coord(tmp_path, active=active, level=70)

    assert await coord.observe_source_volume(observed_source, 30) is False
    assert coord.get_listening_level() == 70


# ---------- source handoff -------------------------------------------------


async def test_handoff_spotify_to_airplay_guards_camilla_before_gate(tmp_path):
    """Push-mode → camilla-master handoff lowers Camilla before mux
    exposes the AirPlay lane."""
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(50)

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert handoff.guard_db == pytest.approx(percent_to_db(50))
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(50))


async def test_handoff_finalize_honors_mute_landed_after_prepare(tmp_path):
    """A remote mute between prepare and finalize must keep the new lane silent."""
    coord, cam, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )
    assert handoff.ok

    # jasper-control handling the remote while mux owns this coordinator's
    # source-transition sequence.
    persistence.save_pre_mute_level(60)

    assert await coord.finalize_source_handoff(handoff) is True
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_handoff_catches_lower_level_during_guard_settle(tmp_path):
    """If the user lowers volume while Camilla is settling, handoff
    catches down before mux opens the target lane."""
    coord, cam, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(50)
    original_set_camilla_db = coord._set_camilla_db
    lowered = False

    async def set_and_lower_once(db, *, context, persist):
        nonlocal lowered
        ok = await original_set_camilla_db(db, context=context, persist=persist)
        if context == "source_handoff_guard" and not lowered:
            persistence.save_listening_level(20, mark_user_change=True)
            lowered = True
        return ok

    coord._set_camilla_db = set_and_lower_once

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert handoff.level == 20
    assert handoff.guard_db == pytest.approx(percent_to_db(20))
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(20))


async def test_handoff_airplay_to_spotify_pushes_before_finalize(tmp_path):
    """Camilla-master → push-mode handoff pushes the source volume
    before mux opens the source, then finalize pins Camilla to 0 dB."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True}, db=-25.0)
    await coord.set_listening_level(60)
    coord.spotify_writes.clear()

    handoff = await coord.prepare_source_handoff(
        Source.AIRPLAY, Source.SPOTIFY, reason="manual",
    )

    assert handoff.ok
    assert handoff.push_ok is True
    assert coord.spotify_writes == [60]
    await coord.finalize_source_handoff(handoff)
    assert cam.set_calls[-1] == pytest.approx(0.0)


async def test_handoff_push_failure_keeps_camilla_guarded(tmp_path):
    """If a push-mode source cannot accept volume, handoff degrades
    safe by keeping downstream Camilla at the canonical guard."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(40)

    async def fail_spotify(_level: int) -> bool:
        return False

    coord._set_spotify = fail_spotify
    handoff = await coord.prepare_source_handoff(
        Source.AIRPLAY, Source.SPOTIFY, reason="manual",
    )

    assert handoff.result == "degraded_safe"
    assert handoff.push_ok is False
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    await coord.finalize_source_handoff(handoff)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))


async def test_observer_transition_push_failure_preserves_guard(tmp_path):
    """The observer backstop must not undo mux's degraded-safe guard.

    If Spotify/Bluetooth cannot accept a source-side volume write,
    Camilla remains the fallback safety carrier instead of being
    cleared to 0 dB on the next active-source observer tick.
    """
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(40)

    async def fail_spotify(_level: int) -> bool:
        return False

    coord._set_spotify = fail_spotify
    coord._backend = _FakeBackend(active={"spotactive": True})

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert cam.set_calls
    assert 0.0 not in cam.set_calls
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))


@pytest.mark.parametrize(
    ("prev_source", "current_source", "setter_name", "context", "pair"),
    [
        (
            Source.AIRPLAY,
            Source.SPOTIFY,
            "_set_spotify",
            "active_source_transition_push_degraded",
            "airplay → spotify",
        ),
        (
            Source.SPOTIFY,
            Source.BLUETOOTH,
            "_set_bluetooth",
            "active_source_transition_push_push_degraded",
            "spotify → bluetooth (push→push)",
        ),
    ],
)
@pytest.mark.parametrize("guard_confirmed", [True, False])
async def test_transition_push_failure_guard_preserves_diagnostics_and_warning(
    tmp_path,
    monkeypatch,
    caplog,
    prev_source: Source,
    current_source: Source,
    setter_name: str,
    context: str,
    pair: str,
    guard_confirmed: bool,
):
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    level = 42
    coord, _, persistence = _coord(tmp_path, active={}, level=level)
    persistence.save_now(-7.5)
    guard_calls = _stub_failed_push(
        monkeypatch, coord, setter_name, guard_confirmed,
    )
    guard_db = percent_to_db(level)

    with caplog.at_level(logging.WARNING, logger=vc_mod.__name__):
        await coord.apply_active_source_transition(prev_source, current_source)

    assert guard_calls == [(pytest.approx(guard_db), context, True)]
    _assert_push_failure_outcome(
        caplog,
        diag_path,
        source=current_source,
        level=level,
        reason="active_source_push_failed",
        context=context,
        guard_confirmed=guard_confirmed,
        unconfirmed_warning=(
            f"active source: {pair}; source volume push failed and camilla "
            f"guard could not be confirmed for {guard_db:.1f} dB"
        ),
    )


async def test_handoff_ducked_camilla_master_waits_until_guard_safe(tmp_path):
    """During a voice duck, a camilla-master target is only safe if the
    current ducked Camilla level is already below the target guard.
    The target is still persisted so Ducker.restore lands safe."""
    coord, cam, persistence = _coord(
        tmp_path,
        active={"spotactive": True},
        selected="airplay",
        db=-25.0,
        level=20,  # target guard is percent_to_db(20); the duck is too loud
        mark_user_change=True,
    )
    persistence.save_now(0.0)

    async def duck_active():
        return True

    coord._duck_active_probe = duck_active

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert not handoff.ok
    assert handoff.detail == "camilla_guard_failed"
    assert cam.set_calls == []
    _assert_persisted(persistence, db=round(percent_to_db(20), 2))


async def test_handoff_ducked_safe_guard_reports_restore_target(tmp_path):
    """If the duck has already made Camilla quiet enough, prepare may
    succeed and Ducker.restore still targets the selected source level."""
    coord, _, _ = _coord(
        tmp_path,
        active={"spotactive": True},
        selected="airplay",
        db=-45.0,
        level=20,
        mark_user_change=True,
    )

    async def duck_active():
        return True

    coord._duck_active_probe = duck_active

    handoff = await coord.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert await coord.get_camilla_target_db() == pytest.approx(percent_to_db(20))


async def test_ducker_restore_preserves_degraded_push_guard(tmp_path):
    """Push-mode normally restores Camilla to 0 dB, but a degraded
    handoff guard is intentional safety state and must survive restore."""
    coord, _, _ = _coord(
        tmp_path, active={"spotactive": True}, selected="spotify",
    )
    await coord.set_listening_level(35)
    guard_db = -32.5
    await coord._set_camilla_db(
        guard_db, context="test_degraded_guard", persist=True,
    )

    assert await coord.get_camilla_target_db() == pytest.approx(guard_db)

    await coord._set_camilla_db(0.0, context="test_normal_push", persist=True)
    assert await coord.get_camilla_target_db() == pytest.approx(0.0)


async def test_set_camilla_deferred_during_voice_session(tmp_path):
    """During a voice session the Ducker owns camilla; coordinator
    writes are deferred, but listening_level still updates so
    Ducker.restore lands at the user's intended level."""
    # Idle backend (camilla carries the level) and an already-ducked fader.
    coord, cam, persistence = _real_coord(tmp_path, active={}, db=-25.0)
    coord.note_voice_session(True)

    await coord.set_listening_level(46)

    assert cam.set_calls == []
    assert coord.get_listening_level() == 46
    _assert_persisted(persistence, level=46)

    coord.note_voice_session(False)
    await coord.set_listening_level(50)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(50))


async def test_fanin_voice_session_keeps_live_camilla_volume_control(tmp_path):
    """Fan-in owns program ducking, not Camilla, so an in-session remote edit
    must land immediately while source transitions remain session-gated."""
    coord, cam, _ = _real_coord(tmp_path, active={}, db=-25.0)
    coord.note_voice_session(True, camilla_volume_locked=False)

    await coord.set_listening_level(46)

    assert cam.set_calls[-1] == pytest.approx(percent_to_db(46))
    before_transition = list(cam.set_calls)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)
    assert cam.set_calls == before_transition


# ---------- volume-context publishing --------------------------------------


async def test_dispatch_publishes_absolute_canonical_and_downstream_facts(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, _, _ = _coord(
        tmp_path,
        active={"spotactive": True},
        volume_context_publisher=publish,
    )

    await coord.set_listening_level(46)

    assert len(published) == 2
    assert all(
        context.canonical_db == pytest.approx(percent_to_db(46))
        for context in published
    )
    assert all(
        context.downstream_db == pytest.approx(0.0) for context in published
    )
    assert all(context.muted is False for context in published)


async def test_nonzero_intent_publishes_before_slow_spotify_dispatch(tmp_path):
    published = []
    cloud_started = asyncio.Event()
    release_cloud = asyncio.Event()

    async def publish(context):
        published.append(context)

    async def blocked_spotify(_level: int) -> bool:
        cloud_started.set()
        await release_cloud.wait()
        return True

    coord, _, _ = _real_coord(
        tmp_path,
        active={"spotactive": True},
        volume_context_publisher=publish,
    )
    coord._set_spotify = blocked_spotify

    operation = asyncio.create_task(coord.set_listening_level(67))
    await wait_signalled(cloud_started, "spotify dispatch started", producer=operation)

    assert len(published) == 1
    assert published[0].canonical_db == pytest.approx(percent_to_db(67))
    assert published[0].muted is False

    release_cloud.set()
    assert await operation == 67
    assert len(published) == 2
    assert published[-1].canonical_db == pytest.approx(percent_to_db(67))
    assert published[-1].muted is False


@pytest.mark.parametrize("blocker", ["source_push", "camilla_mute"])
async def test_mute_intent_is_local_and_published_before_the_slow_write(
    tmp_path, blocker,
):
    """The mute is local intent. Whichever downstream write is slow — the
    Spotify cloud round trip or Camilla's own main_mute — the muted context
    is already published before it returns."""
    published = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def publish(context):
        published.append(context)

    coord, cam, _ = _real_coord(
        tmp_path,
        active={"spotactive": True} if blocker == "source_push" else {},
        db=percent_to_db(59),
        level=59,
        volume_context_publisher=publish,
    )
    if blocker == "source_push":
        async def blocked_spotify(_level: int) -> bool:
            started.set()
            await release.wait()
            return True

        coord._set_spotify = blocked_spotify
    else:
        real_set_mute = coord._set_camilla_main_mute
        first_call = True

        async def blocked_set_mute(target: bool, *, context: str) -> bool:
            nonlocal first_call
            if first_call:
                first_call = False
                started.set()
                await release.wait()
            return await real_set_mute(target, context=context)

        coord._set_camilla_main_mute = blocked_set_mute

    operation = asyncio.create_task(coord.mute())
    await wait_signalled(started, "slow downstream write started", producer=operation)

    if blocker == "source_push":
        # Camilla's mute has already landed; only the source push is slow.
        assert cam.muted is True
    assert len(published) == 1
    assert published[0].muted is True

    release.set()
    assert await operation == 59
    assert len(published) == 2
    assert published[-1].muted is True


async def test_overlapping_push_writes_keep_source_persistence_and_context_aligned(
    tmp_path,
):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    persistence.save_listening_level(50)
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    applied = []
    published = []

    async def push(level: int) -> bool:
        if level == 20:
            first_started.set()
            await release_first.wait()
        applied.append(level)
        return True

    async def publish(context):
        published.append(context)

    first = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        volume_context_publisher=publish,
    )
    second = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        volume_context_publisher=publish,
    )
    first._set_spotify = push
    second._set_spotify = push

    older = asyncio.create_task(first.set_listening_level(20))
    await wait_signalled(first_started, "older push write started", producer=older)
    newer = asyncio.create_task(second.set_listening_level(80))
    await asyncio.sleep(0)
    assert newer.done() is False
    release_first.set()
    assert await older == 20
    assert await newer == 80

    record = persistence.load()
    assert record is not None
    newest_context = max(published, key=lambda context: context.stamp_boot_ns)
    assert applied[-1] == 80
    assert record.listening_level == 80
    assert newest_context.canonical_db == pytest.approx(percent_to_db(80))


@pytest.mark.parametrize("camilla_readable", [True, False])
async def test_persisted_mute_intent_outranks_what_camilla_reports(
    tmp_path, camilla_readable,
):
    """A stale unmuted readback — or no readback at all — cannot resurrect
    audio the owner muted."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={}, db=percent_to_db(59), level=59,
    )
    persistence.save_pre_mute_level(59)
    cam.muted = False
    if not camilla_readable:
        async def unreadable():
            return None, None

        coord._read_camilla_volume_and_mute = unreadable

    context = await coord.effective_volume_context()

    assert context.muted is True
    if camilla_readable:
        assert context.canonical_db == pytest.approx(percent_to_db(59))


async def test_unmute_and_push_mode_nonzero_publish_unmuted_context(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, cam, persistence = _real_coord(
        tmp_path,
        active={},
        db=percent_to_db(59),
        level=59,
        volume_context_publisher=publish,
    )
    persistence.save_pre_mute_level(59)
    cam.muted = True

    await coord.unmute()
    assert published[-1].muted is False

    push = VolumeCoordinator(
        camilla=_FakeCamilla(db=0.0),
        persistence=persistence,
        backend=_FakeBackend(active={"spotactive": True}),
    )
    assert (await push.effective_volume_context()).muted is False


async def test_publisher_failure_never_breaks_volume_operation(tmp_path):
    async def fail(_context):
        raise OSError("fanin unavailable")

    coord, _, _ = _coord(tmp_path)
    coord._volume_context_publisher = fail
    assert await coord.set_listening_level(47) == 47


async def test_context_snapshot_retries_after_concurrent_volume_change(tmp_path):
    coord, _, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(30), level=30,
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    real_read = coord._read_camilla_volume_and_mute
    first = True

    async def blocked_read():
        nonlocal first
        if first:
            first = False
            read_started.set()
            await release_read.wait()
        return await real_read()

    coord._read_camilla_volume_and_mute = blocked_read
    snapshot = asyncio.create_task(coord.effective_volume_context())
    await wait_signalled(
        read_started, "camilla volume/mute read started", producer=snapshot,
    )
    await coord.set_listening_level(80)
    release_read.set()
    context = await snapshot

    assert context.canonical_db == pytest.approx(percent_to_db(80))
    assert context.downstream_db == pytest.approx(percent_to_db(80))


async def test_context_snapshot_stamp_is_bound_before_slow_probe(
    tmp_path, monkeypatch,
):
    coord, _, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(30), level=30,
    )
    stamp_bound = False

    def bind_stamp():
        nonlocal stamp_bound
        stamp_bound = True
        return 123

    real_read = coord._read_camilla_volume_and_mute

    async def verify_stamp_precedes_probe():
        assert stamp_bound is True
        return await real_read()

    monkeypatch.setattr(
        "jasper.volume_coordinator.volume_context_stamp_boot_ns", bind_stamp,
    )
    coord._read_camilla_volume_and_mute = verify_stamp_precedes_probe

    context = await coord.effective_volume_context()

    assert context.stamp_boot_ns == 123


# ---- cross-daemon duck-active probe -------------------------------------
#
# jasper-control builds a fresh VolumeCoordinator per HTTP request, so the
# in-process `_voice_session_active` flag is always False there even when
# jasper-voice has a session in flight. Those coordinators get a
# `duck_active_probe` callable that asks jasper-voice over UDS whether the
# Ducker is engaged. The probe is the authoritative signal — no inference.


@pytest.mark.parametrize(
    ("answer", "expect_defer"),
    [
        pytest.param("true", True, id="duck_active"),
        pytest.param("false", False, id="no_duck"),
        pytest.param("none", False, id="probe_unreachable"),
        pytest.param("raises", False, id="probe_raises"),
        pytest.param("absent", False, id="no_probe_configured"),
    ],
)
async def test_the_duck_probe_answer_decides_whether_the_camilla_write_defers(
    tmp_path, caplog, answer, expect_defer,
):
    """Only a probe that says "ducking" defers the fader write.

    Everything else fails open — an unreachable UDS, a wedged voice daemon, a
    malformed reply, even a probe that raises — because a home appliance is
    better off un-ducking music for a moment than leaving the owner with a
    dead remote over an inter-daemon problem. jasper-voice's own coordinator
    configures no probe and uses `_voice_session_active` instead.
    """
    answers = {"true": True, "false": False, "none": None}

    async def probe():
        if answer == "raises":
            raise RuntimeError("simulated probe bug")
        return answers[answer]

    coord, cam, persistence = _real_coord(
        tmp_path,
        active={},
        db=-40.0,
        duck_active_probe=None if answer == "absent" else probe,
    )

    with caplog.at_level(logging.WARNING, logger=vc_mod.__name__):
        await coord.set_listening_level(70)

    if expect_defer:
        assert cam.set_calls == []
    else:
        assert cam.set_calls[-1] == pytest.approx(percent_to_db(70))
    # Either way the level persists, so Ducker.restore lands at user intent.
    assert coord.get_listening_level() == 70
    _assert_persisted(persistence, level=70)
    # A misbehaving probe has no structured counterpart; the warning is it.
    assert any(
        "duck_active_probe raised" in message for message in _warnings(caplog)
    ) is (answer == "raises")


async def test_set_camilla_fast_spin_regression(tmp_path):
    """Fast remote spin batching 3 detents (+12% / +6 dB) with no session.

    The old dB-comparison heuristic read that as an `inferred_duck`, deferred,
    and persisted listening_level while main_volume stayed put — so every
    later twist read the inflated level and deferred again, trapping the user
    with a knob that did nothing until they spun all the way down.
    """
    async def probe():
        return False

    coord, cam, _ = _real_coord(
        tmp_path,
        active={},
        db=-18.0,  # in sync with listening_level=64%, per the production log
        level=64,
        mark_user_change=True,
        duck_active_probe=probe,
    )

    await coord.adjust_listening_level(12)

    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(76))
    assert coord.get_listening_level() == 76

    # No cascade: subsequent small twists keep tracking 1:1.
    await coord.adjust_listening_level(4)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(80))
    assert coord.get_listening_level() == 80


async def test_set_camilla_defer_logs_session_signaled_event(tmp_path, caplog):
    """The probe-driven defer is distinguishable in the journal from the
    in-process flag path and from any future defer reason."""
    async def probe():
        return True

    coord, _, _ = _real_coord(
        tmp_path, active={}, db=-40.0, duck_active_probe=probe,
    )

    caplog.set_level(logging.INFO, logger=vc_mod.__name__)
    await coord.set_listening_level(70)

    fields = _event_fields(caplog, "volume.deferred")
    assert fields["reason"] == "session_signaled"
    assert fields["level"] == "70%"
    assert fields["target_db"] == f"{percent_to_db(70):.1f}"


# ---- maybe_reconcile_camilla (self-healing backstop) --------------------
#
# The reconciler runs at 1 Hz inside VolumeObserver._tick and converges
# main_volume_db back toward percent_to_db(listening_level) when they have
# drifted, catching any other writer or transient that creates a desync.


@pytest.mark.parametrize(
    ("current_db", "level", "writes"),
    [
        # Camilla's own jitter / sub-percentile rounding.
        pytest.param(percent_to_db(70) - 0.3, 70, False, id="dead_band"),
        pytest.param(-18.0, 76, True, id="quiet_drift"),
        pytest.param(-8.0, 70, True, id="loud_drift"),
        # Deep LOUD drift is unsafe in a way deep quiet is not.
        pytest.param(0.0, 0, True, id="deep_loud_drift"),
        # The retained quiet carve-out: a fader claim held in ANOTHER process
        # — jasper-web's volume-floor audition — parks camilla tens of dB
        # below the household level with nothing this reconciler can ask
        # (#3038).
        pytest.param(percent_to_db(70) - 25.0, 70, False, id="deep_quiet_drift"),
    ],
)
async def test_which_drift_the_reconciler_corrects(
    tmp_path, current_db, level, writes,
):
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=current_db, level=level, mark_user_change=True,
    )

    await coord.maybe_reconcile_camilla()

    if writes:
        assert cam.set_calls == [pytest.approx(percent_to_db(level))]
        assert cam.mute_calls == [level == 0]
    else:
        assert cam.set_calls == []
        assert cam.mute_calls == []


async def test_reconcile_revalidates_after_cross_daemon_volume_change(tmp_path):
    """A stale preflight cannot overwrite a newer user command.

    The observer and control daemon have separate coordinator/persistence
    instances in production. The reconciler may begin a Camilla read just
    before jasper-control lowers the volume; once it joins the shared
    operation lease, it must re-read both canonical intent and Camilla instead
    of writing its stale, louder target.
    """
    path = str(tmp_path / "speaker_volume.json")
    observer_persistence = VolumePersistence(path)
    observer_persistence.save_listening_level(60, mark_user_change=True)
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    observer = VolumeCoordinator(
        camilla=cam, persistence=observer_persistence, backend=backend,
    )
    control = VolumeCoordinator(
        camilla=cam, persistence=VolumePersistence(path), backend=backend,
    )
    read_started = asyncio.Event()
    release_stale_read = asyncio.Event()
    original_read = observer._read_camilla_volume_and_mute
    read_count = 0

    async def stale_first_read():
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            read_started.set()
            await release_stale_read.wait()
            return 0.0, False
        return await original_read()

    observer._read_camilla_volume_and_mute = stale_first_read
    reconcile = asyncio.create_task(observer.maybe_reconcile_camilla())
    await wait_signalled(
        read_started,
        "reconcile began its first Camilla read",
        producer=reconcile,
    )

    await control.set_listening_level(20)
    control_write_count = len(cam.set_calls)
    release_stale_read.set()
    await reconcile

    assert cam._db == pytest.approx(percent_to_db(20))
    assert len(cam.set_calls) == control_write_count
    _assert_persisted(
        observer_persistence, level=20, db=percent_to_db(20), db_abs=0.01,
    )


async def test_reconcile_repairs_zero_percent_mute_drift(tmp_path):
    """At 0%, matching dB is not enough; main_mute must also be true."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={}, db=-50.0, level=0, mark_user_change=True,
    )
    persistence.save_now(-50.0)

    await coord.maybe_reconcile_camilla()

    # The fader already carried the floor, so the owner leaves it alone and
    # only the mute is repaired — which is what this test is about.
    assert cam._db == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_a_measurement_claim_outranks_a_household_volume_set(tmp_path):
    """The household write is a CLAIM now, and it can be outranked.

    A crossover-v2 session holds the fader at the level its excitation-safety
    ledger admitted each program against. A volume twist landing inside that
    session must not move the speaker out from under the stimulus — and must
    not be thrown away either: it is what the fader lands on at release.
    """
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(40), level=40,
    )
    await coord._write_camilla_db_with_mute(percent_to_db(40), context="test_seed")
    claim = await coord.volume_owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, -12.5,
    )
    assert cam._db == pytest.approx(-12.5)
    cam.set_calls.clear()

    await coord.set_listening_level(70)

    assert cam.set_calls == []
    assert cam._db == pytest.approx(-12.5)
    assert coord.get_listening_level() == 70

    await coord.volume_owner.release(claim)

    assert cam._db == pytest.approx(percent_to_db(70))


async def test_reconcile_preserves_toggle_mute_restore_level(tmp_path):
    """Toggle mute persists the restore level separately from audible 0%.

    The voice daemon's 1 Hz reconciler must treat `pre_mute_level` as the
    active mute intent. Otherwise it sees listening_level=59%, expects
    main_mute=false, and immediately undoes a remote mute button press.
    """
    coord, cam, persistence = _real_coord(
        tmp_path, active={}, db=percent_to_db(0), level=59, mark_user_change=True,
    )
    cam.muted = True
    persistence.save_now(percent_to_db(0))
    persistence.save_pre_mute_level(59)

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls == []
    assert cam.mute_calls == []


async def _gate_voice_session(coord, cam):
    coord.note_voice_session(True)


async def _gate_measurement(coord, cam):
    await coord.note_measurement_active(True)


async def _gate_camilla_offline(coord, cam):
    cam.unavailable = True


async def _gate_none(coord, cam):
    return None


@pytest.mark.parametrize(
    ("active", "gate"),
    [
        pytest.param({}, _gate_voice_session, id="voice_session"),
        pytest.param({}, _gate_measurement, id="measurement_active"),
        pytest.param({}, _gate_camilla_offline, id="camilla_unreachable"),
        pytest.param({"spotactive": True}, _gate_none, id="push_mode_source"),
    ],
)
async def test_the_reconciler_stands_down_behind_each_gate(tmp_path, active, gate):
    """Voice session → the Ducker owns camilla. Measurement → correction's
    ramp lease owns it. Push-mode source → camilla is pinned at 0 dB by
    design and the level lives on the source's own slider. Camilla
    unreachable → skip silently and retry on the next tick.

    Camilla sits 15 dB LOUDER than the level implies in every case — the
    direction the reconciler always corrects, per
    test_which_drift_the_reconciler_corrects[loud_drift] — so only the gate
    can hold the write back. None of them may write or raise.
    """
    coord, cam, _ = _real_coord(
        tmp_path,
        active=active,
        db=0.0,
        level=70,
        mark_user_change=True,
    )
    await gate(coord, cam)

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls == []
    assert cam.mute_calls == []


async def test_reconcile_in_flight_stops_when_measurement_begins(tmp_path):
    """MEASURE_PAUSE may race a tick already awaiting Camilla readback."""
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=-3.15, level=70, mark_user_change=True,
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def blocked_read():
        read_started.set()
        await release_read.wait()
        return -3.15, False

    coord._read_camilla_volume_and_mute = blocked_read
    reconcile = asyncio.create_task(coord.maybe_reconcile_camilla())
    await wait_signalled(
        read_started, "camilla volume/mute read started", producer=reconcile,
    )
    await coord.note_measurement_active(True)
    release_read.set()
    await reconcile

    assert cam.set_calls == []


async def test_measurement_pause_waits_for_in_flight_reconcile_write(tmp_path):
    """Pause acknowledges only after an older Camilla write has finished."""
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=-3.15, level=70, mark_user_change=True,
    )
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    original_set = cam.set_volume_db

    async def blocked_set(db, *, best_effort=False):
        write_started.set()
        await release_write.wait()
        return await original_set(db, best_effort=best_effort)

    cam.set_volume_db = blocked_set
    reconcile = asyncio.create_task(coord.maybe_reconcile_camilla())
    await wait_signalled(
        write_started, "camilla volume write started", producer=reconcile,
    )
    pause = asyncio.create_task(coord.note_measurement_active(True))
    await asyncio.sleep(0)
    assert not pause.done()

    release_write.set()
    await reconcile
    await pause
    writes_at_acquire = len(cam.set_calls)
    await coord.maybe_reconcile_camilla()

    assert writes_at_acquire == 1
    assert len(cam.set_calls) == writes_at_acquire


async def test_reconcile_emits_structured_event(tmp_path, caplog):
    """The reconciler's write carries enough context that a debugger can
    answer "who caused the drift" from journalctl alone."""
    coord, _, _ = _real_coord(
        tmp_path, active={}, db=-18.0, level=76, mark_user_change=True,
    )

    caplog.set_level(logging.INFO, logger=vc_mod.__name__)
    await coord.maybe_reconcile_camilla()

    fields = _event_fields(caplog, "volume.reconciled")
    assert fields["level"] == "76%"
    assert fields["current_db"] == "-18.00"
    assert fields["expected_db"] == f"{percent_to_db(76):.2f}"
    assert fields["drift_db"] == f"{percent_to_db(76) - (-18.0):+.2f}"


async def test_reconcile_no_loop_when_already_converged(tmp_path):
    """After one reconcile fires and camilla is at expected, the next
    tick must be a no-op (no write loop)."""
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=-18.0, level=76, mark_user_change=True,
    )
    await coord.maybe_reconcile_camilla()
    first_write_count = len(cam.set_calls)
    assert first_write_count == 1

    await coord.maybe_reconcile_camilla()

    assert len(cam.set_calls) == first_write_count


# ---------- duck restore target --------------------------------------------


@pytest.mark.parametrize(
    ("active", "level", "expected"),
    [
        pytest.param({}, 70, percent_to_db(70), id="idle"),
        pytest.param({"aplactive": True}, 40, percent_to_db(40), id="airplay"),
        pytest.param({"spotactive": True}, 70, 0.0, id="push_mode"),
        pytest.param(
            {"spotactive": True}, 0, percent_to_db(0), id="push_mode_at_zero",
        ),
    ],
)
async def test_the_duck_restore_target_follows_the_active_carrier(
    tmp_path, active, level, expected,
):
    """Camilla-master sources restore to the household level; push-mode
    restores to camilla's 0 dB carrier because the source's own slider holds
    the level — except at 0%, where restore must not unmask a content mute."""
    coord, _, persistence = _real_coord(tmp_path, active=active, level=level)
    # A persisted attenuation would mean a degraded handoff guard, which is a
    # deliberate exception pinned by
    # test_ducker_restore_preserves_degraded_push_guard.
    persistence.save_now(0.0)

    assert await coord.get_camilla_target_db() == pytest.approx(expected)


async def test_get_camilla_target_db_uses_effective_temporary_mute(tmp_path):
    """Every carrier path interprets remembered-level + mute through VolumeState."""
    coord, _, persistence = _real_coord(
        tmp_path, active={}, db=percent_to_db(0), level=70,
    )
    persistence.save_pre_mute_level(70)

    assert await coord.get_camilla_target_db() == pytest.approx(percent_to_db(0))


async def test_get_camilla_target_db_refreshes_from_disk(tmp_path):
    """Cross-process staleness guard for the duck-restore path.

    The control daemon writes listening_level to disk on every twist;
    voice-daemon's in-memory `_level` only auto-refreshes on its own
    set/adjust/mute/transition calls. Without the refresh, Ducker.restore()
    at the end of a wake writes camilla to the stale dB — observed as a 56 dB
    jump at duck-off after a remote spin landed between voice operations.
    """
    coord, _, persistence = _coord(
        tmp_path, active={"aplactive": True}, level=38,
    )
    persistence.save_listening_level(80)  # the control daemon, another process

    assert await coord.get_camilla_target_db() == pytest.approx(percent_to_db(80))
    assert coord.get_listening_level() == 80


async def test_transition_refreshes_from_disk(tmp_path):
    """The same cross-process staleness guard on the transition path, which
    is observer-triggered and so never refreshes as a side effect."""
    coord, _, persistence = _coord(
        tmp_path, active={"aplactive": True}, level=50,
    )
    persistence.save_listening_level(80)  # the control daemon, another process
    coord._backend = _FakeBackend(active={"spotactive": True})

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert coord.spotify_writes == [80]
    assert coord.get_listening_level() == 80


async def test_transition_uses_effective_level_while_temporarily_muted(tmp_path):
    coord, _, persistence = _coord(
        tmp_path,
        active={"spotactive": True},
        db=percent_to_db(0),
        level=80,
    )
    persistence.save_pre_mute_level(80)

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert coord.spotify_writes == [0]
    assert coord.get_volume_state().restore_percent == 80


# ---------- camilla restart-blip survival ---------------------------------


async def test_volume_coordinator_proceeds_when_camilla_unreachable(tmp_path):
    """A remote twist arriving during a 2 s camilla restart blip must not
    throw: the user's intent is preserved end-to-end so the next operation
    lands at the right level once camilla is back."""
    coord, cam, persistence = _real_coord(tmp_path, active={})
    cam.unavailable = True

    assert await coord.set_listening_level(70) == 70

    assert coord.get_listening_level() == 70
    _assert_persisted(persistence, level=70)
    # best_effort=True silently dropped the write while the fake was down.
    assert cam.set_calls == []

    cam.unavailable = False
    await coord.set_listening_level(40)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    assert coord.get_listening_level() == 40


# ---------- USB sink (camilla-master, host-slider observed inbound) --------


async def test_set_volume_usbsink_active_routes_to_camilla(tmp_path):
    """USB sink behaves like AirPlay for outbound: remote/voice writes
    land on CamillaDSP. The gadget mixer is NOT written back to (the
    host's slider is observed-only)."""
    coord, cam, _ = _coord(tmp_path, active={"usbsinkactive": True})
    await coord.set_listening_level(60)
    assert coord.camilla_writes == [60]
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(60))
    assert coord.spotify_writes == []
    assert coord.bt_writes == []


async def test_observe_usbsink_updates_listening_level_when_active(tmp_path):
    """Host slider moves while USB is the active source — listening
    level follows and CamillaDSP, the USB carrier, is updated."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={"usbsinkactive": True}, level=80,
    )

    accepted = await coord.observe_source_volume(Source.USBSINK, 45)

    assert accepted is True
    assert coord.get_listening_level() == 45
    _assert_persisted(persistence, level=45)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(45))
    assert cam.mute_calls[-1] is False


async def test_observe_usbsink_initial_snapshot_cannot_clear_remote_mute(tmp_path):
    """Bridge activation/restart state yields to an already-latched mute."""
    coord, cam, persistence = _real_coord(
        tmp_path,
        active={"usbsinkactive": True},
        db=percent_to_db(60),
        level=60,
    )
    persistence.save_mute_state(60, "remote-mute")
    cam.muted = True

    accepted = await coord.observe_source_volume(Source.USBSINK, 60, initial=True)

    assert accepted is False
    state = coord.get_volume_state()
    assert state.effective_percent == 0
    assert state.restore_percent == 60

    # A later changed host value is explicit intent and may end the mute.
    assert await coord.observe_source_volume(Source.USBSINK, 65) is True
    assert coord.get_volume_state().effective_percent == 65


@pytest.mark.parametrize(
    ("active", "selected"),
    [
        pytest.param({"usbsinkactive": True}, None, id="raw_activity_probe"),
        # Mux selection is the speaker gate, so USB host volume follows it
        # even while the raw usbsink RMS activity probe is quiet.
        pytest.param({}, "usbsink", id="mux_selection_probe_idle"),
    ],
)
async def test_observe_usbsink_unmute_restores_camilla_carrier(
    tmp_path, active, selected,
):
    """A host unmute restores the slider value; because USB is
    camilla-master, that observation must raise Camilla back — volume
    first, then main_mute=false."""
    coord, cam, persistence = _real_coord(
        tmp_path, active=active, selected=selected, db=-50.0, level=0,
    )
    persistence.save_now(-50.0)
    cam.muted = True

    await coord.observe_source_volume(Source.USBSINK, 75)

    assert coord.get_listening_level() == 75
    _assert_persisted(persistence, level=75, db=round(percent_to_db(75), 2))
    assert cam.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(75))),
        ("mute", False),
    ]


async def test_observe_usbsink_unmute_defers_during_duck(tmp_path):
    """A USB host unmute records intent but does not clobber active ducking."""
    async def probe():
        return True

    coord, cam, persistence = _real_coord(
        tmp_path,
        active={"usbsinkactive": True},
        db=-50.0,
        level=0,
        duck_active_probe=probe,
    )
    persistence.save_now(-50.0)
    cam.muted = True

    await coord.observe_source_volume(Source.USBSINK, 75)

    assert coord.get_listening_level() == 75
    _assert_persisted(persistence, level=75, db=percent_to_db(0))
    assert cam.set_calls == []
    assert cam.mute_calls[-1] is False


@pytest.mark.parametrize(
    "db",
    [
        pytest.param(-20.0, id="fader_drifted"),
        pytest.param(percent_to_db(0), id="fader_at_floor_but_unmuted"),
    ],
)
async def test_observe_usbsink_same_level_repairs_camilla_drift(tmp_path, db):
    """An observation at the level JTS already remembers still reconverges
    Camilla instead of returning early. At 0% convergence means BOTH the
    floor dB and main_mute, so whichever half drifted gets repaired."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={"usbsinkactive": True}, db=db, level=0,
    )
    persistence.save_now(db)

    await coord.observe_source_volume(Source.USBSINK, 0)

    assert coord.get_listening_level() == 0
    assert cam._db == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True
    _assert_persisted(persistence, db=percent_to_db(0))


async def test_equal_usbsink_observation_publishes_repaired_downstream(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, _, persistence = _real_coord(
        tmp_path,
        active={"usbsinkactive": True},
        db=-20.0,
        level=50,
        volume_context_publisher=publish,
    )
    persistence.save_now(-20.0)

    await coord.observe_source_volume(Source.USBSINK, 50)

    assert len(published) == 1
    assert published[0].downstream_db == pytest.approx(percent_to_db(50))


async def test_observe_usbsink_clamps_out_of_range(tmp_path):
    """Defensive: percent outside [0, 100] gets clamped before storage."""
    coord, _, _ = _real_coord(
        tmp_path, active={"usbsinkactive": True}, level=50,
    )

    await coord.observe_source_volume(Source.USBSINK, 150)
    assert coord.get_listening_level() == 100

    await coord.observe_source_volume(Source.USBSINK, -20)
    assert coord.get_listening_level() == 0


async def test_usbsink_is_camilla_master(tmp_path):
    """`_camilla_carries_level` decides whether camilla keeps the user's
    perceived level or is pinned at 0 dB."""
    coord, _, _ = _coord(tmp_path, active={"usbsinkactive": True})
    assert await coord._camilla_carries_level(Source.USBSINK) is True
    assert await coord._camilla_carries_level(Source.SPOTIFY) is False


# ---------- bluealsa transport-path probe goes through shared backoff -------
#
# _bluez_alsa_active_transport_path runs in jasper-control on every BT
# volume set from the remote/web. It must reuse jasper.bluealsa_probe so a
# D-Bus permission denial backs off process-wide instead of hammering the
# system bus once per volume set. These tests fail if the helper reverts
# to its own raw `bluealsa-cli list-pcms` subprocess.


def _fake_pcm_list(monkeypatch, stdout: bytes, returncode: int = 0) -> dict[str, int]:
    """Stand in for `bluealsa-cli list-pcms`, counting spawns."""
    calls = {"n": 0}

    class _Proc:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self):
            return stdout, b"permission denied" if returncode else b""

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    return calls


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param(
            b"/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source PCM ...\n",
            "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source",
            id="one_transport",
        ),
        pytest.param(b"", None, id="no_transport"),
    ],
)
async def test_bluez_transport_path_parses_the_pcm_list(
    monkeypatch, stdout, expected,
):
    _fake_pcm_list(monkeypatch, stdout)

    assert await vc_mod._bluez_alsa_active_transport_path() == expected


@pytest.mark.parametrize(
    "tripped_by_another_consumer",
    [pytest.param(False, id="own_failure"), pytest.param(True, id="shared_backoff")],
)
async def test_bluez_transport_path_honours_the_shared_probe_backoff(
    monkeypatch, tripped_by_another_consumer,
):
    """The backoff is process-wide: a D-Bus rejection recorded by ANY
    bluealsa_probe consumer short-circuits this helper's next probe without
    spawning. Pins the 'shared module', not a per-caller, contract."""
    calls = _fake_pcm_list(monkeypatch, b"", returncode=1)
    if tripped_by_another_consumer:
        bluealsa_probe.note_probe_failure("rc=1", vc_mod.logger)
        expected_spawns = 0
    else:
        assert await vc_mod._bluez_alsa_active_transport_path() is None
        expected_spawns = 1

    assert await vc_mod._bluez_alsa_active_transport_path() is None
    assert calls["n"] == expected_spawns


# ---------- graph-swap duck vs. the 1 Hz reconciler -------------------------


class _MinimalCamillaClient:
    """Just enough pycamilladsp surface to run a REAL `CamillaController`."""

    def __init__(self, db: float) -> None:
        self.volume = self
        self.config = self
        self.general = self
        self.db = float(db)
        self.muted = False
        self.reload_count = 0

    def main_volume(self) -> float:
        return self.db

    def main_mute(self) -> bool:
        return self.muted

    def set_main_volume(self, value: float) -> None:
        self.db = float(value)

    def set_main_mute(self, value: bool) -> None:
        self.muted = bool(value)

    def reload(self) -> None:
        self.reload_count += 1


def _real_controller(client: _MinimalCamillaClient, tmp_path):
    from jasper.camilla import CamillaController

    cam = CamillaController("127.0.0.1", 1234)
    cam._graph_mutation_lock_path = tmp_path / ".dsp_apply.lock"

    async def call(fn):
        return fn(client)

    cam._call = call  # type: ignore[method-assign]
    return cam


def _owned_coord(tmp_path, db: float):
    """Recording coordinator over a REAL CamillaController and volume owner."""
    client = _MinimalCamillaClient(db=db)
    cam = _real_controller(client, tmp_path)
    coord = _RecordingCoordinator(
        camilla=cam,
        persistence=VolumePersistence(str(tmp_path / "speaker_volume.json")),
        backend=_FakeBackend(active={}),
    )
    return coord, cam, client


async def test_a_reconcile_tick_cannot_outrank_a_held_transient_duck(tmp_path):
    """The reconciler writes by DECLARING the household level, so a duck held
    in this process outranks it — no dB inference is involved, which is why
    `RECONCILE_DUCK_SKIP_DB` is not what protects `CueDuck`.

    The duck is shallower than that threshold, so the carve-out cannot be
    what spares it; releasing the claim lands the fader back on the household
    level.
    """
    expected_db = percent_to_db(70)
    coord, _, client = _owned_coord(tmp_path, db=expected_db)
    await coord.set_listening_level(70)
    owner = coord.volume_owner
    await owner.declare_household_level_db(expected_db)
    claim = await owner.acquire_duck(5.0)
    assert owner.holds_kind(ClaimKind.TRANSIENT_DUCK)
    ducked_db = client.db
    assert ducked_db == pytest.approx(expected_db - 5.0)

    await coord.maybe_reconcile_camilla()

    assert client.db == pytest.approx(ducked_db)

    await owner.release(claim)
    assert client.db == pytest.approx(expected_db)


async def test_reconciler_stands_down_while_a_dsp_writer_holds_the_graph(tmp_path):
    """The swap's claim on the fader is the writer lock, not the duck's depth.

    The realistic shape: a household volume change lands during the bracket,
    so the ducked fader now reads LOUDER than the level the reconciler
    expects — the one direction it always corrects. Only the lock can hold
    that write back, and once the lock goes the very next tick corrects it,
    which is what makes the stand-down transient rather than a second
    carve-out.
    """
    from jasper.dsp_apply import camilla_graph_mutation

    expected_db = percent_to_db(40)
    coord, cam, client = _owned_coord(tmp_path, db=expected_db)
    await coord.set_listening_level(40)
    client.db = 0.0  # some other writer left camilla far too loud

    async with camilla_graph_mutation(
        source="test.swap", lock_path=cam._graph_mutation_lock_path,
    ):
        await coord.maybe_reconcile_camilla()
        assert client.db == pytest.approx(0.0)

    await coord.maybe_reconcile_camilla()
    assert client.db == pytest.approx(expected_db)


async def test_reconciler_still_corrects_a_drift_louder_than_expected(tmp_path):
    """The quiet carve-out is directional — it must never turn the
    reconciler's loud-direction safety correction into a skip."""
    expected_db = percent_to_db(40)
    coord, _, client = _owned_coord(tmp_path, db=expected_db)
    await coord.set_listening_level(40)

    client.db = 0.0  # some other writer left camilla far too loud
    await coord.maybe_reconcile_camilla()

    assert client.db == pytest.approx(expected_db)


# ---------- graph-swap duck composed with CueDuck ---------------------------

# enter/exit sequences for the two holders. The two orders the review probed
# are `bracket_first_cue_last` and `cue_first_bracket_last`; the other two are
# the strictly-nested cases they bracket.
_INTERLEAVINGS = {
    "bracket_first_cue_last": ["B_enter", "C_enter", "B_exit", "C_exit"],
    "bracket_outer_cue_inner": ["B_enter", "C_enter", "C_exit", "B_exit"],
    "cue_first_bracket_last": ["C_enter", "B_enter", "C_exit", "B_exit"],
    "cue_outer_bracket_inner": ["C_enter", "B_enter", "B_exit", "C_exit"],
}


@pytest.mark.parametrize("order", sorted(_INTERLEAVINGS))
async def test_cue_and_graph_swap_interleave_back_to_the_canonical_target(
    order, tmp_path, monkeypatch,
):
    """After ANY interleaving, once both holders have exited, the fader is at
    the canonical target — and it is never above it while either still holds.

    Replaying entry snapshots stranded it instead: whichever holder exited last
    wrote back a value the other had already ducked, tens of dB quiet, in the
    one band `maybe_reconcile_camilla` deliberately refuses to heal.
    """
    from jasper import camilla as camilla_module

    monkeypatch.setattr(camilla_module, "MAIN_VOLUME_RAMP_SETTLE_S", 0.0)
    canonical_db = percent_to_db(70)
    coord, cam, client = _owned_coord(tmp_path, db=canonical_db)
    await coord.set_listening_level(70)
    monkeypatch.setattr(
        camilla_module,
        "_canonical_target_db_provider",
        coord.get_camilla_target_db,
    )

    cue = camilla_module.CueDuck(coord.volume_owner, -25.0)
    bracket = cam._graph_mutation("test.swap")
    steps = {
        "B_enter": bracket.__aenter__,
        "B_exit": lambda: bracket.__aexit__(None, None, None),
        "C_enter": cue.__aenter__,
        "C_exit": lambda: cue.__aexit__(None, None, None),
    }
    for step in _INTERLEAVINGS[order]:
        await steps[step]()
        assert client.db <= canonical_db + 1e-6, (
            f"after {step} the fader sat above the canonical target — a duck "
            "released something it did not apply"
        )

    assert client.db == pytest.approx(canonical_db), (
        f"{order} left the fader stranded at {client.db:.1f} dB "
        f"(canonical {canonical_db:.1f} dB)"
    )


async def test_duck_release_never_lands_above_a_volume_change_made_inside_it(
    tmp_path, monkeypatch,
):
    """A user volume change inside the bracket is what rules out a bare
    relative release: giving back 40 dB on top of the level the coordinator
    just wrote lands tens of dB above what the user asked for. The canonical
    ceiling is the half that prevents it.
    """
    from jasper import camilla as camilla_module

    monkeypatch.setattr(camilla_module, "MAIN_VOLUME_RAMP_SETTLE_S", 0.0)
    coord, cam, client = _owned_coord(tmp_path, db=percent_to_db(70))
    await coord.set_listening_level(70)
    monkeypatch.setattr(
        camilla_module,
        "_canonical_target_db_provider",
        coord.get_camilla_target_db,
    )

    bracket = cam._graph_mutation("test.swap")
    await bracket.__aenter__()
    await coord.set_listening_level(30)
    lowered_db = percent_to_db(30)
    await bracket.__aexit__(None, None, None)

    assert client.db == pytest.approx(lowered_db), (
        f"the release landed at {client.db:.1f} dB, not the {lowered_db:.1f} dB "
        "the user asked for during the swap"
    )
