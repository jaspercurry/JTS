# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared test doubles for jasper.volume_coordinator's tests.

Every double sits at a public boundary, so a test reads the same when the
coordinator's internals move: a CamillaDSP fake whose settings stand in for
each way the real controller answers, a renderer backend fake, the recorder
patched onto ``volume_push_sources``' two push functions, the coordinator
builders, and the minimal pycamilladsp client that runs a REAL
``CamillaController``. Consumed by name by tests/test_volume_coordinator.py and
tests/test_sound_setup.py.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

from jasper import volume_push_sources as vps_mod
from jasper.camilla import CamillaController, CamillaUnavailable
from jasper.music_sources import Source
from jasper.volume_coordinator import VolumeCoordinator
from jasper.volume_persistence import VolumePersistence

_REAL_PUSHES = (vps_mod.push_spotify_volume, vps_mod.push_bluetooth_volume)


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
        # Awaited before each volume+mute read; a non-None answer is what
        # Camilla reports instead of its real state.
        self.read_hook: Callable[[], Awaitable[tuple[float, bool] | None]] | None = None
        # Awaited before each main_mute write, with the requested flag.
        self.mute_hook: Callable[[bool], Awaitable[None]] | None = None
        # False: Camilla refuses main_mute writes (best_effort answers False).
        self.mute_accepted = True
        # Raised, in order, by the next main_volume writes whatever best_effort.
        self.write_errors: list[BaseException] = []

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
        if self.read_hook is not None:
            reported = await self.read_hook()
            if reported is not None:
                return reported
        if self.unavailable:
            if best_effort:
                return None
            raise CamillaUnavailable("test fake offline")
        return self._db, self.muted

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        if self.write_errors:
            raise self.write_errors.pop(0)
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
        if self.mute_hook is not None:
            await self.mute_hook(bool(muted))
        if self.unavailable or not self.mute_accepted:
            if best_effort:
                return False
            raise CamillaUnavailable("test fake offline")
        self.muted = bool(muted)
        self.mute_calls.append(bool(muted))
        self.events.append(("mute", bool(muted)))
        return True


class _FakeBackend:
    """Renderer answers; an exception as ``active`` is a probe that raises.
    Mux reports no handoff, so every observer transition applies."""

    def __init__(
        self,
        active: dict[str, bool] | Exception | None = None,
        selected: str | None = None,
    ) -> None:
        self._active = active or {}
        self._selected = selected
        self.active_renderers_calls = 0

    async def active_renderers(self) -> dict[str, bool]:
        self.active_renderers_calls += 1
        if isinstance(self._active, Exception):
            raise self._active
        return dict(self._active)

    async def selected_source(self) -> str | None:
        return self._selected

    async def last_handoff(self) -> dict | None:
        return None


class _Pushes:
    """Every source push, answered through ``volume_push_sources``' own names.

    ``ok`` is each source's answer; ``hook`` is awaited inside each push after
    it is recorded, so a test can hold a slow cloud or AVRCP write open.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Source, int]] = []
        self.ok = {Source.SPOTIFY: True, Source.BLUETOOTH: True}
        self.hook: Callable[[Source, int], Awaitable[None]] | None = None

    @property
    def spotify(self) -> list[int]:
        return [level for source, level in self.calls if source is Source.SPOTIFY]

    @property
    def bluetooth(self) -> list[int]:
        return [level for source, level in self.calls if source is Source.BLUETOOTH]

    async def _push(self, source: Source, level: int) -> bool:
        self.calls.append((source, level))
        if self.hook is not None:
            await self.hook(source, level)
        return self.ok[source]

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch) -> _Pushes:
        pushes = cls()

        async def push_spotify(_router, _device_name: str, level: int) -> bool:
            return await pushes._push(Source.SPOTIFY, level)

        async def push_bluetooth(level: int) -> bool:
            return await pushes._push(Source.BLUETOOTH, level)

        monkeypatch.setattr(vps_mod, "push_spotify_volume", push_spotify)
        monkeypatch.setattr(vps_mod, "push_bluetooth_volume", push_bluetooth)
        return pushes


@pytest.fixture(autouse=True)
def pushes(monkeypatch: pytest.MonkeyPatch) -> _Pushes:
    """Every Spotify/Bluetooth push, delivered unless a test refuses it."""
    return _Pushes.install(monkeypatch)


def _use_real_pushes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the recorder, for a test whose subject is the push functions."""
    monkeypatch.setattr(vps_mod, "push_spotify_volume", _REAL_PUSHES[0])
    monkeypatch.setattr(vps_mod, "push_bluetooth_volume", _REAL_PUSHES[1])


def _build(
    tmp_path,
    *,
    active: dict[str, bool] | Exception | None = None,
    selected: str | None = None,
    backend: _FakeBackend | None = None,
    db: float = 0.0,
    level: int | None = None,
    mark_user_change: bool = False,
    **kwargs,
):
    """Coordinator over a fresh on-disk record; returns (coord, cam, store).

    ``level`` seeds both the persisted level and the coordinator's view of it,
    which is what a coordinator that has already served a set looks like.
    """
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=db)
    coord = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend or _FakeBackend(active=active, selected=selected),
        **kwargs,
    )
    if level is not None:
        persistence.save_listening_level(
            level, mark_user_change=mark_user_change,
        )
        coord.load_persisted_level()
    return coord, cam, persistence


def _coord(tmp_path, **kwargs):
    """Coordinator whose handoffs skip the settle wait."""
    kwargs.setdefault("handoff_settle_sec", 0.0)
    return _build(tmp_path, **kwargs)


def _real_coord(tmp_path, **kwargs):
    """Coordinator with the production handoff timings."""
    return _build(tmp_path, **kwargs)


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


class _MinimalCamillaClient:
    """Just enough pycamilladsp surface to run a REAL `CamillaController`."""

    def __init__(self, db: float) -> None:
        self.volume = SimpleNamespace(
            main_volume=self.main_volume, main_mute=self.main_mute,
            set_main_volume=self.set_main_volume, set_main_mute=self.set_main_mute,
        )
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
    cam = CamillaController("127.0.0.1", 1234)
    cam._graph_mutation_lock_path = tmp_path / ".dsp_apply.lock"

    async def call(fn):
        return fn(client)

    cam._call = call  # type: ignore[method-assign]
    return cam


def _owned_coord(tmp_path, db: float):
    """Coordinator over a REAL CamillaController and volume owner."""
    client = _MinimalCamillaClient(db=db)
    cam = _real_controller(client, tmp_path)
    coord = VolumeCoordinator(
        camilla=cam,
        persistence=VolumePersistence(str(tmp_path / "speaker_volume.json")),
        backend=_FakeBackend(active={}),
    )
    return coord, cam, client
