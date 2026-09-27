# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Byte-identity harness for the ``volume_coordinator.py`` split (R-0VC, #4806).

Restored from ``20b9c38ff3^:tests/volume_coordinator_trace.py`` and extended
per the R-0VC plan's Proof paragraph. It lives OUTSIDE the repo and is run
against a checkout:

    PYTHONPATH=<checkout> <venv>/bin/python volume_coordinator_trace.py \
        [--dump FILE] [--names FILE] [--expect-root <checkout>]

One ordered stream records, as they happen:

* every ``jasper.*`` log record (level + rendered message);
* every CamillaDSP call, through a REAL ``CamillaController`` whose ``_call``
  runs the lambda against a minimal client (the shape of
  ``tests/test_volume_coordinator.py``'s ``_real_controller``), so every
  ``_coerce_main_volume_db`` clamp decision shows;
* every persisted body (``VolumePersistence``'s atomic write);
* every source push (``volume_push_sources.push_*`` patched at the module);
* every published ``EffectiveVolumeContext``;
* every probe answer (mux selection, renderer activity, duck probe, the
  DSP-writer flock probe, jasper-control's measurement hold);
* each step's return value (or raised type) and the process ``VolumeOwner``
  ``declared_level_db()`` after it.

Clocks are frozen: ``time.monotonic`` / ``time.clock_gettime_ns`` are replaced
BEFORE jasper is imported (so import-time captures such as
``_measurement_monotonic = time.monotonic`` see the fake), the event loop keeps
the real clock, ``datetime.now`` is frozen in the two volume modules that read
it, and ``uuid4`` tokens are numbered per scenario.

The digest is SHA-256 over ``json.dumps(stream, sort_keys=True)``. Logger
NAMES are deliberately not in the digest (a move may rename a logger); they
are written to ``--names`` so a move can list every rename.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import enum
import hashlib
import inspect
import itertools
import json
import logging
import os
import sys
import tempfile
import threading  # noqa: F401 - imported before the clock patch on purpose
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

# ---------------------------------------------------------------- environment
TMP = tempfile.mkdtemp(prefix="vctrace-")
os.environ["JASPER_SOUND_SETTINGS_PATH"] = os.path.join(TMP, "settings.json")
os.environ["JASPER_VOLUME_DIAGNOSTICS_PATH"] = os.path.join(TMP, "volume_policy.json")
os.environ["JASPER_VOLUME_STATE_PATH"] = os.path.join(TMP, "unused_state.json")
os.environ.pop("JASPER_LOG_JSON", None)

# ---------------------------------------------------------------- the clocks
_REAL_MONOTONIC = time.monotonic
MONO_EPOCH = 5000.0
WALL_EPOCH = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


class _Clock:
    mono = MONO_EPOCH


CLOCK = _Clock()


def _fake_monotonic() -> float:
    return CLOCK.mono


def _fake_clock_gettime_ns(_clock_id: int) -> int:
    return int(round(CLOCK.mono * 1_000_000_000))


time.monotonic = _fake_monotonic  # type: ignore[assignment]
time.clock_gettime_ns = _fake_clock_gettime_ns  # type: ignore[assignment]


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        value = WALL_EPOCH + timedelta(seconds=CLOCK.mono - MONO_EPOCH)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


def advance(seconds: float) -> None:
    CLOCK.mono += seconds


class _RealClockLoop(asyncio.SelectorEventLoop):
    def time(self) -> float:  # asyncio's timers keep real time
        return _REAL_MONOTONIC()


# ---------------------------------------------------------------- jasper
import jasper  # noqa: E402
from jasper import camilla as camilla_mod  # noqa: E402
from jasper import volume_echo as echo_mod  # noqa: E402
from jasper import volume_persistence as vp_mod  # noqa: E402
from jasper import volume_push_sources as push_mod  # noqa: E402
from jasper.atomic_io import advisory_file_lock  # noqa: E402
from jasper.camilla import CamillaController, CamillaUnavailable  # noqa: E402
from jasper.music_sources import Source  # noqa: E402
from jasper.platform import control_client  # noqa: E402
from jasper.platform.control_client import ControlError  # noqa: E402
from jasper.voice.measurement_hold import MEASUREMENT_AUTOCLEAR_SEC  # noqa: E402
from jasper.volume_coordinator import VolumeCoordinator  # noqa: E402
from jasper.volume_persistence import VolumePersistence  # noqa: E402

vp_mod.datetime = FrozenDatetime  # type: ignore[misc]
echo_mod.datetime = FrozenDatetime  # type: ignore[misc]

_REAL_UUID4 = uuid.uuid4
_UUIDS = itertools.count(1)


def _numbered_uuid4() -> uuid.UUID:
    return uuid.UUID(int=next(_UUIDS))


def _patch_uuid4() -> None:
    uuid.uuid4 = _numbered_uuid4  # type: ignore[assignment]
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "") or ""
        if name.startswith("jasper") and getattr(module, "uuid4", None) is _REAL_UUID4:
            module.uuid4 = _numbered_uuid4  # type: ignore[attr-defined]


_patch_uuid4()

# ---------------------------------------------------------------- the stream
STREAM: list = []
NAMES: dict[str, set] = {}


def rec(kind: str, *payload) -> None:
    STREAM.append([kind, *payload])


def r(value):
    return None if value is None else round(float(value), 4)


def norm(value):
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, float):
        return r(value)
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): norm(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [type(value).__name__] + [
            [f.name, norm(getattr(value, f.name))] for f in dataclasses.fields(value)
        ]
    return value


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage().replace(TMP, "<tmp>")
        STREAM.append(["log", record.levelname, message])
        key = (
            message.split(" ", 1)[0]
            if message.startswith("event=")
            else str(record.msg).replace(TMP, "<tmp>")
        )
        NAMES.setdefault(key, set()).add(record.name)


_JASPER_LOGGER = logging.getLogger("jasper")
_JASPER_LOGGER.setLevel(logging.DEBUG)
_JASPER_LOGGER.addHandler(_Capture())
_JASPER_LOGGER.propagate = False


# ---------------------------------------------------------------- fakes
class Fx:
    """Per-world scripted answers for every fake the coordinator reaches."""

    def __init__(self) -> None:
        self.offline = False
        self.writes_fail = False
        self.writes_raise = False
        self.graph_probe = "real"  # "real" | "raises"
        self.duck: object = False  # True | False | None | "raises"
        self.hold = "free"  # "free" | "held" | "unreadable" | "malformed"
        self.push_ok = {"spotify": True, "bluetooth": True}
        self.block_push: tuple[str, int] | None = None
        self.push_started = asyncio.Event()
        self.release_push = asyncio.Event()
        self.block_read = 0  # block this many reads (get_volume_and_mute)
        self.read_started = asyncio.Event()
        self.release_read = asyncio.Event()
        self.block_write = 0  # block this many set_main_volume writes
        self.write_started = asyncio.Event()
        self.release_write = asyncio.Event()
        self.on_read = None  # callable run before each volume+mute read
        self.read_values: list = []  # fader dB the client reads next, in order
        self.publisher = "ok"  # "ok" | "unsent" | "raises"


FX = Fx()


class Client:
    """Just enough pycamilladsp surface to run a REAL CamillaController."""

    def __init__(self, db: float, muted: bool) -> None:
        self.db = float(db)
        self.muted = bool(muted)
        self.volume = SimpleNamespace(
            main_volume=self.main_volume,
            main_mute=self.main_mute,
            set_main_volume=self.set_main_volume,
            set_main_mute=self.set_main_mute,
        )

    def main_volume(self) -> float:
        rec("cam.read_volume", r(self.db))
        return self.db

    def main_mute(self) -> bool:
        rec("cam.read_mute", self.muted)
        return self.muted

    def set_main_volume(self, value: float) -> None:
        rec("cam.set_volume", r(value))
        self.db = float(value)

    def set_main_mute(self, value: bool) -> None:
        rec("cam.set_mute", bool(value))
        self.muted = bool(value)


def make_controller(client: Client, lock_path: Path) -> CamillaController:
    cam = CamillaController("127.0.0.1", 1234)
    cam._graph_mutation_lock_path = lock_path

    async def call(fn):
        name = getattr(fn, "__qualname__", "?").split(".")[1]
        write = name.startswith("set_")
        if FX.offline:
            rec("cam.offline", name)
            raise CamillaUnavailable("trace: camilla offline")
        if write and FX.writes_fail:
            rec("cam.refused", name)
            raise CamillaUnavailable("trace: write refused")
        if write and FX.writes_raise:
            rec("cam.raised", name)
            raise RuntimeError("trace: write raised")
        if name == "get_volume_and_mute":
            if FX.on_read is not None:
                FX.on_read()
            if FX.read_values:
                client.db = FX.read_values.pop(0)
            if FX.block_read:
                FX.block_read -= 1
                FX.read_started.set()
                await FX.release_read.wait()
        if name == "set_volume_db" and FX.block_write:
            FX.block_write -= 1
            FX.write_started.set()
            await FX.release_write.wait()
        return fn(client)

    cam._call = call  # type: ignore[method-assign]
    real_probe = cam.graph_mutation_in_progress

    def probe():
        if FX.graph_probe == "raises":
            rec("probe.dsp_writer", "raises")
            raise RuntimeError("trace: flock probe failed")
        answer = real_probe()
        rec("probe.dsp_writer", answer)
        return answer

    cam.graph_mutation_in_progress = probe  # type: ignore[method-assign]
    return cam


class Backend:
    def __init__(self, active: dict | None, selected: str | None) -> None:
        self.active = dict(active or {})
        self.selected = selected
        self.selected_script: list = []

    async def selected_source(self):
        answer = self.selected_script.pop(0) if self.selected_script else self.selected
        rec("probe.mux", answer)
        if answer == "raises":
            raise RuntimeError("trace: mux unreachable")
        return answer

    async def active_renderers(self):
        rec("probe.renderers", sorted(k for k, v in self.active.items() if v))
        return dict(self.active)


async def duck_probe():
    answer = FX.duck
    rec("probe.duck", answer)
    if answer == "raises":
        raise RuntimeError("trace: duck probe bug")
    return answer


async def publisher(context) -> bool:
    rec(
        "publish",
        r(context.canonical_db),
        r(context.downstream_db),
        r(context.tts_envelope_lufs),
        bool(context.muted),
        context.stamp_boot_ns,
    )
    if FX.publisher == "raises":
        raise OSError("trace: fan-in socket refused")
    return FX.publisher == "ok"


async def push_spotify_volume(router, device_name, level):
    rec("push.spotify", router, device_name, level)
    if FX.block_push == ("spotify", level):
        FX.push_started.set()
        await FX.release_push.wait()
    ok = FX.push_ok["spotify"]
    rec("push.result", "spotify", ok)
    return ok


async def push_bluetooth_volume(level):
    rec("push.bluetooth", level)
    if FX.block_push == ("bluetooth", level):
        FX.push_started.set()
        await FX.release_push.wait()
    ok = FX.push_ok["bluetooth"]
    rec("push.result", "bluetooth", ok)
    return ok


push_mod.push_spotify_volume = push_spotify_volume
push_mod.push_bluetooth_volume = push_bluetooth_volume


def get_measurement(**kwargs):
    rec("probe.hold", FX.hold, kwargs.get("timeout"))
    if FX.hold == "unreadable":
        raise ControlError("trace: connection refused")
    if FX.hold == "malformed":
        return ["not", "a", "dict"]
    if FX.hold == "held":
        return {"active": True, "owner": "trace-measurement"}
    return {"active": False}


control_client.get_measurement = get_measurement

_REAL_ATOMIC_WRITE_TEXT = vp_mod.atomic_write_text


def _recording_atomic_write_text(path, text, **kwargs):
    rec("persist", os.path.basename(os.fspath(path)), json.loads(text))
    return _REAL_ATOMIC_WRITE_TEXT(path, text, **kwargs)


vp_mod.atomic_write_text = _recording_atomic_write_text


# ---------------------------------------------------------------- worlds
class World:
    def __init__(
        self,
        name: str,
        *,
        active: dict | None = None,
        selected: str | None = None,
        db: float = 0.0,
        muted: bool = False,
        level: int | None = None,
        mark_user_change: bool = False,
        duck: bool = False,
        publish: bool = True,
        record: dict | None = None,
    ) -> None:
        global FX
        FX = Fx()
        CLOCK.mono = MONO_EPOCH
        global _UUIDS
        _UUIDS = itertools.count(1)
        rec("scenario", name)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP, prefix="w"))
        self.state_path = self.tmp / "speaker_volume.json"
        if record is not None:
            self.state_path.write_text(json.dumps(record), encoding="utf-8")
        self.lock_path = self.tmp / ".dsp_apply.lock"
        self.client = Client(db, muted)
        self.cam = make_controller(self.client, self.lock_path)
        self.backend = Backend(active, selected)
        self.persistence = VolumePersistence(str(self.state_path))
        self.duck = duck
        self.publish = publish
        self.coord = self.make_coord(self.persistence)
        if level is not None:
            self.persistence.save_listening_level(level, mark_user_change=mark_user_change)
            self.coord.load_persisted_level()

    def make_coord(self, persistence: VolumePersistence | None = None) -> VolumeCoordinator:
        return VolumeCoordinator(
            camilla=self.cam,
            persistence=persistence or VolumePersistence(str(self.state_path)),
            backend=self.backend,
            spotify_router="trace-router",
            spotify_device_name="JTS-trace",
            duck_active_probe=duck_probe if self.duck else None,
            volume_context_publisher=publisher if self.publish else None,
            handoff_settle_sec=0.0,
            push_settle_sec=0.0,
        )

    def other_writer(self) -> VolumePersistence:
        """Another process's handle on the same state file."""
        return VolumePersistence(str(self.state_path))


async def step(coord: VolumeCoordinator, name: str, make) -> object:
    try:
        value = make()
        if inspect.isawaitable(value):
            value = await value
        outcome = ["ok", norm(value)]
    except Exception as e:  # noqa: BLE001 - the trace records whatever escapes
        value = None
        outcome = ["raised", type(e).__name__, str(e)]
    rec("step", name, outcome, r(coord.volume_owner.declared_level_db()))
    return value


async def steps(coord, table) -> None:
    for name, make in table:
        await step(coord, name, make)


def verb_table(c: VolumeCoordinator) -> list:
    return [
        ("set:70", lambda: c.set_listening_level(70)),
        ("set:1", lambda: c.set_listening_level(1)),
        ("set:0", lambda: c.set_listening_level(0)),
        ("set:35", lambda: c.set_listening_level(35)),
        ("set:100", lambda: c.set_listening_level(100)),
        ("set:-5", lambda: c.set_listening_level(-5)),
        ("set:150", lambda: c.set_listening_level(150)),
        ("adjust:-15", lambda: c.adjust_listening_level(-15)),
        ("adjust:+30", lambda: c.adjust_listening_level(30)),
        ("adjust:-200", lambda: c.adjust_listening_level(-200)),
        ("adjust:+7", lambda: c.adjust_listening_level(7)),
        ("set:60", lambda: c.set_listening_level(60)),
        ("mute", lambda: c.mute()),
        ("mute_again", lambda: c.mute()),
        ("is_muted", lambda: c.is_muted()),
        ("state", lambda: c.get_volume_state()),
        *[
            (f"revision:{s.value}", lambda s=s: c.source_observation_revision(s))
            for s in Source
        ],
        ("target_db_muted", lambda: c.get_camilla_target_db()),
        ("context_muted", lambda: c.effective_volume_context()),
        ("toggle", lambda: c.toggle_mute()),
        ("toggle", lambda: c.toggle_mute()),
        ("set_muted:True", lambda: c.set_muted(True)),
        ("set_muted:False", lambda: c.set_muted(False)),
        ("set_muted:False", lambda: c.set_muted(False)),
        ("unmute_no_latch", lambda: c.unmute(fallback_level=33)),
        ("mute", lambda: c.mute()),
        ("unmute", lambda: c.unmute()),
        ("set:0", lambda: c.set_listening_level(0)),
        ("mute_at_zero", lambda: c.mute()),
        ("unmute_from_zero", lambda: c.unmute(fallback_level=44)),
        ("target_db", lambda: c.get_camilla_target_db()),
        ("context", lambda: c.effective_volume_context()),
        ("publish", lambda: c.publish_volume_context(phase="trace")),
        ("listening", lambda: c.get_listening_level()),
        ("load", lambda: c.load_persisted_level()),
        ("owner_declared", lambda: c.volume_owner.declared_level_db()),
    ]


SOURCE_PROFILES = [
    ("idle", {}, None),
    ("airplay", {"aplactive": True}, None),
    ("spotify", {"spotactive": True}, None),
    ("bluetooth", {"btactive": True}, None),
    ("usbsink", {"usbsinkactive": True}, None),
    ("mux_spotify_over_airplay", {"aplactive": True}, "spotify"),
    ("mux_idle_over_spotify", {"spotactive": True}, "idle"),
    ("mux_lane_label", {"btactive": True}, "correction"),
    ("mux_raises", {"usbsinkactive": True}, "raises"),
    ("all_active", {"aplactive": True, "spotactive": True, "btactive": True, "usbsinkactive": True}, None),
]


# ---------------------------------------------------------------- scenarios
async def s_verbs() -> None:
    for name, active, selected in SOURCE_PROFILES:
        w = World(f"verbs:{name}", active=active, selected=selected, db=-20.0, level=50)
        await step(w.coord, "initialize", lambda: w.coord.initialize())
        await steps(w.coord, verb_table(w.coord))
    for source, key in (("spotify", "spotactive"), ("bluetooth", "btactive")):
        for variant in ("push_fail", "push_fail_camilla_offline", "push_fail_duck", "push_fail_duck_unknown"):
            w = World(
                f"verbs:{source}:{variant}", active={key: True}, db=-7.5, level=70,
                duck=variant.startswith("push_fail_duck"),
            )
            FX.push_ok[source] = False
            if variant == "push_fail_camilla_offline":
                FX.offline = True
            if variant == "push_fail_duck":
                FX.duck = True
            if variant == "push_fail_duck_unknown":
                FX.duck = None
            await steps(w.coord, verb_table(w.coord))
    # The two "unsent" / failing publisher shapes.
    for mode in ("unsent", "raises"):
        w = World(f"verbs:publisher_{mode}", active={"spotactive": True}, level=40)
        FX.publisher = mode
        await steps(w.coord, [
            ("set:55", lambda: w.coord.set_listening_level(55)),
            ("mute", lambda: w.coord.mute()),
            ("unmute", lambda: w.coord.unmute()),
            ("publish", lambda: w.coord.publish_volume_context()),
        ])
    w = World("verbs:no_publisher", active={"spotactive": True}, level=40, publish=False)
    await steps(w.coord, [
        ("set:55", lambda: w.coord.set_listening_level(55)),
        ("mute", lambda: w.coord.mute()),
        ("publish", lambda: w.coord.publish_volume_context()),
    ])


async def s_observe() -> None:
    # 1. No native mapping.
    w = World("observe:unmapped", active={}, level=50)
    await step(w.coord, "observe:idle", lambda: w.coord.observe_source_volume(Source.IDLE, 42))
    # 2. Not the active source (optimistic check).
    w = World("observe:inactive", active={"spotactive": True}, level=70)
    await step(w.coord, "observe:airplay", lambda: w.coord.observe_source_volume(Source.AIRPLAY, 30))
    await step(w.coord, "observe:usbsink", lambda: w.coord.observe_source_volume(Source.USBSINK, 30))
    # 3. Source moved while the observation queued for the lease.
    w = World("observe:moved_at_lease", active={}, selected="spotify", level=60)
    w.backend.selected_script = ["spotify", "bluetooth"]
    await step(w.coord, "observe:spotify", lambda: w.coord.observe_source_volume(Source.SPOTIFY, 40))
    await step(w.coord, "state", lambda: w.coord.get_volume_state())
    # 4. A measurement holds the fader: only the emergency zero lands.
    for source, key in ((Source.USBSINK, "usbsinkactive"), (Source.SPOTIFY, "spotactive")):
        w = World(f"observe:measuring:{source.value}", active={key: True}, db=-20.0, level=60)
        await step(w.coord, "measure_on", lambda: w.coord.note_measurement_active(True))
        await step(w.coord, "observe:40", lambda: w.coord.observe_source_volume(source, 40))
        await step(w.coord, "observe:0", lambda: w.coord.observe_source_volume(source, 0))
        await step(w.coord, "state", lambda: w.coord.get_volume_state())
    # 5. An initial snapshot yields to a latched mute (camilla-master sources).
    for source, key in ((Source.USBSINK, "usbsinkactive"), (Source.AIRPLAY, "aplactive")):
        w = World(f"observe:initial_latched:{source.value}", active={key: True}, db=-8.0, level=60)
        w.other_writer().save_mute_state(60, "remote-mute")
        await step(w.coord, "observe_initial:60", lambda: w.coord.observe_source_volume(source, 60, initial=True))
        await step(w.coord, "observe_initial_unlatched?", lambda: w.coord.get_volume_state())
        advance(3.0)
        await step(w.coord, "observe:65", lambda: w.coord.observe_source_volume(source, 65))
        await step(w.coord, "observe_initial:65", lambda: w.coord.observe_source_volume(source, 65, initial=True))
    # 6. Push-mode: a latch without a token is repaired, zero confirms it.
    for source, key, zero, nonzero in (
        (Source.SPOTIFY, "spotactive", 0, 65),
        (Source.BLUETOOTH, "btactive", 0, 64),
    ):
        w = World(f"observe:push_latch:{source.value}", active={key: True}, db=0.0, level=60)
        w.other_writer().save_mute_state(60, None)
        await step(w.coord, "revision", lambda: w.coord.source_observation_revision(source))
        await step(w.coord, "observe:nonzero_before_zero", lambda: w.coord.observe_source_volume(source, nonzero))
        await step(w.coord, "revision", lambda: w.coord.source_observation_revision(source))
        await step(w.coord, "observe:zero", lambda: w.coord.observe_source_volume(source, zero))
        await step(w.coord, "observe:zero_again", lambda: w.coord.observe_source_volume(source, zero))
        await step(w.coord, "observe:nonzero", lambda: w.coord.observe_source_volume(source, nonzero))
        await step(w.coord, "state", lambda: w.coord.get_volume_state())
        # A NEW token needs its own zero.
        other = w.other_writer()
        other.save_mute_state(None, None)
        other.save_listening_level(60)
        other.save_mute_state(60, "mute-b")
        await step(w.coord, "observe:token_b_nonzero", lambda: w.coord.observe_source_volume(source, nonzero))
        await step(w.coord, "state", lambda: w.coord.get_volume_state())
        await step(w.coord, "observe:token_b_initial_zero", lambda: w.coord.observe_source_volume(source, zero, initial=True))
    # 7. Own echo, then outside the window.
    for observed in (60, 30):
        w = World(f"observe:echo:{observed}", active={"spotactive": True}, level=50)
        await step(w.coord, "set:60", lambda: w.coord.set_listening_level(60))
        advance(0.4)
        await step(w.coord, "observe_in_window", lambda: w.coord.observe_source_volume(Source.SPOTIFY, observed))
        advance(0.2)
        await step(w.coord, "observe_edge", lambda: w.coord.observe_source_volume(Source.SPOTIFY, observed))
        advance(5.0)
        await step(w.coord, "observe_outside", lambda: w.coord.observe_source_volume(Source.SPOTIFY, observed))
    # 8. Another process's recent write outranks a stale poll.
    w = World("observe:cross_process", active={"spotactive": True}, level=70, mark_user_change=True)
    advance(10.0)
    w.other_writer().save_listening_level(80)
    await step(w.coord, "observe:70_stale", lambda: w.coord.observe_source_volume(Source.SPOTIFY, 70))
    await step(w.coord, "listening", lambda: w.coord.get_listening_level())
    await step(w.coord, "observe:80_equal", lambda: w.coord.observe_source_volume(Source.SPOTIFY, 80))
    advance(2.0)
    await step(w.coord, "observe:55_edge", lambda: w.coord.observe_source_volume(Source.SPOTIFY, 55))
    advance(0.5)
    await step(w.coord, "observe:55_after", lambda: w.coord.observe_source_volume(Source.SPOTIFY, 55))
    # 9. Equal-level observations repair the carrier (camilla-master and push).
    for name, active, source, db, muted, level, persisted_db, duck in (
        ("usbsink_drift", {"usbsinkactive": True}, Source.USBSINK, -20.0, False, 50, -20.0, None),
        ("usbsink_converged", {"usbsinkactive": True}, Source.USBSINK, None, False, 50, None, None),
        ("usbsink_zero_unmuted", {"usbsinkactive": True}, Source.USBSINK, "floor", False, 0, "floor", None),
        ("usbsink_camilla_offline", {"usbsinkactive": True}, Source.USBSINK, -20.0, False, 50, None, None),
        ("airplay_mute_drift", {"aplactive": True}, Source.AIRPLAY, None, True, 50, None, None),
        ("spotify_guard", {"spotactive": True}, Source.SPOTIFY, -25.0, False, 100, -25.0, None),
        ("spotify_live_guard", {"spotactive": True}, Source.SPOTIFY, -13.0, False, 90, 0.0, None),
        ("spotify_guard_duck", {"spotactive": True}, Source.SPOTIFY, -13.0, False, 90, -13.0, True),
        ("spotify_guard_duck_unknown", {"spotactive": True}, Source.SPOTIFY, -13.0, False, 90, -13.0, None),
        ("spotify_muted_flag", {"spotactive": True}, Source.SPOTIFY, 0.0, True, 70, None, None),
        ("bluetooth_clean", {"btactive": True}, Source.BLUETOOTH, 0.0, False, 50, None, None),
        ("usbsink_drift_exact_minus", {"usbsinkactive": True}, Source.USBSINK, "level-1", False, 50, None, None),
        ("usbsink_drift_exact_plus", {"usbsinkactive": True}, Source.USBSINK, "level+1", False, 50, None, None),
        ("spotify_guard_boundary_muted", {"spotactive": True}, Source.SPOTIFY, 0.0, True, 70, -1.0, None),
        ("spotify_guard_past_boundary_muted", {"spotactive": True}, Source.SPOTIFY, 0.0, True, 70, -1.01, None),
        ("spotify_live_guard_boundary", {"spotactive": True}, Source.SPOTIFY, -1.0, False, 70, 0.0, None),
        ("spotify_live_guard_past_boundary", {"spotactive": True}, Source.SPOTIFY, -1.01, False, 70, 0.0, None),
        ("spotify_persisted_guard_boundary", {"spotactive": True}, Source.SPOTIFY, 0.0, False, 70, -1.0, None),
    ):
        from jasper.volume_curve import percent_to_db
        floor = percent_to_db(0)
        cam_db = {
            "floor": floor,
            None: percent_to_db(level),
            "level-1": percent_to_db(level) - 1.0,
            "level+1": percent_to_db(level) + 1.0,
        }[db] if db is None or isinstance(db, str) else db
        w = World(
            f"observe:equal:{name}", active=active, db=cam_db, muted=muted, level=level,
            duck=duck is not None,
        )
        if duck is not None:
            FX.duck = duck
        if persisted_db is not None:
            w.other_writer().save_now(floor if persisted_db == "floor" else persisted_db)
        if name == "usbsink_camilla_offline":
            FX.offline = True
        native = level if source != Source.BLUETOOTH else 64
        await step(w.coord, "observe:equal", lambda: w.coord.observe_source_volume(source, native))
        await step(w.coord, "observe:equal_again", lambda: w.coord.observe_source_volume(source, native))
    # 10. User-side changes on every mapped source, incl. clamps and uint16.
    for source, key, values in (
        (Source.AIRPLAY, "aplactive", (30, 0, 101, -3)),
        (Source.USBSINK, "usbsinkactive", (45, 150, -20, 0, 75)),
        (Source.SPOTIFY, "spotactive", (40, 0, 100)),
        (Source.BLUETOOTH, "btactive", (64, 0, 127, 200)),
    ):
        for duck in (False, True):
            w = World(
                f"observe:user:{source.value}:duck={duck}", active={key: True},
                db=-50.0, muted=True, level=0, duck=duck,
            )
            w.other_writer().save_now(-50.0)
            FX.duck = duck
            for value in values:
                advance(3.0)
                await step(w.coord, f"observe:{value}", lambda v=value: w.coord.observe_source_volume(source, v))
    # 11. Two coordinators: a queued stale nonzero cannot cancel a mute.
    w = World("observe:two_coordinator_race", active={"spotactive": True}, db=0.0, level=50)
    control = w.coord
    observer = w.make_coord()
    await step(control, "control_set:60", lambda: control.set_listening_level(60))
    advance(3.0)
    FX.block_push = ("spotify", 0)
    mute_task = asyncio.create_task(control.mute())
    await asyncio.wait_for(FX.push_started.wait(), timeout=10)
    rec("race", "mute_push_blocked")
    observation = asyncio.create_task(observer.observe_source_volume(Source.SPOTIFY, 60))
    await asyncio.sleep(0)
    rec("race", "observation_pending", observation.done())
    FX.release_push.set()
    saved = await mute_task
    accepted = await observation
    rec("race", "resolved", saved, accepted)
    await step(observer, "observer_state", lambda: observer.get_volume_state())
    await step(observer, "observe:0", lambda: observer.observe_source_volume(Source.SPOTIFY, 0))
    await step(observer, "observe:65", lambda: observer.observe_source_volume(Source.SPOTIFY, 65))
    await step(observer, "observer_state", lambda: observer.get_volume_state())
    await step(control, "control_state", lambda: control.get_volume_state())


HANDOFF_PAIRS = [
    (Source.IDLE, Source.SPOTIFY),
    (Source.SPOTIFY, Source.AIRPLAY),
    (Source.SPOTIFY, Source.BLUETOOTH),
    (Source.AIRPLAY, Source.USBSINK),
    (Source.AIRPLAY, Source.AIRPLAY),
    (Source.BLUETOOTH, Source.IDLE),
    (Source.USBSINK, Source.BLUETOOTH),
]

HANDOFF_VARIANTS = {
    # name: (camilla db, camilla muted, level, push ok, offline, duck answer)
    "loud_camilla": (0.0, False, 40, True, False, None),
    "quiet_camilla": (-45.0, False, 40, True, False, None),
    "mute_drift": ("level", True, 40, True, False, None),
    "push_fail": (0.0, False, 40, False, False, None),
    "push_fail_offline": (0.0, False, 40, False, True, None),
    "duck_unsafe": (-5.0, False, 20, True, False, True),
    "duck_safe": (-45.0, False, 20, True, False, True),
    "muted_latch": (0.0, False, "muted", True, False, None),
}


async def s_handoff() -> None:
    from jasper.volume_curve import percent_to_db
    for prev, cur in HANDOFF_PAIRS:
        for variant, (db, muted, level, push_ok, offline, duck) in HANDOFF_VARIANTS.items():
            for ending in ("finalize", "abort"):
                seed_level = 60 if level == "muted" else level
                cam_db = percent_to_db(seed_level) if db == "level" else db
                w = World(
                    f"handoff:{prev.value}>{cur.value}:{variant}:{ending}",
                    active={}, selected=cur.value, db=cam_db, muted=muted, level=seed_level,
                    duck=duck is not None,
                )
                if level == "muted":
                    w.other_writer().save_mute_state(60, "latched")
                FX.push_ok = {"spotify": push_ok, "bluetooth": push_ok}
                FX.offline = offline
                if duck is not None:
                    FX.duck = duck
                async with w.coord.source_handoff_operation():
                    h = await step(w.coord, "prepare", lambda: w.coord.prepare_source_handoff(prev, cur, reason="trace"))
                    if h is not None:
                        await step(w.coord, ending, lambda: getattr(w.coord, f"{ending}_source_handoff")(h))
                await step(w.coord, "target_db", lambda: w.coord.get_camilla_target_db())
    # The level moves between prepare and finalize (lower, then higher).
    for cur, moves in ((Source.SPOTIFY, (10, 80)), (Source.AIRPLAY, (10, 80))):
        for moved in moves:
            for push_after in (True, False):
                w = World(
                    f"handoff:moved:{cur.value}:{moved}:push_after={push_after}",
                    active={}, selected=cur.value, db=0.0, level=40,
                )
                h = await step(w.coord, "prepare", lambda: w.coord.prepare_source_handoff(Source.IDLE if cur == Source.SPOTIFY else Source.SPOTIFY, cur, reason="trace"))
                w.other_writer().save_listening_level(moved)
                FX.push_ok = {"spotify": push_after, "bluetooth": push_after}
                await step(w.coord, "finalize", lambda: w.coord.finalize_source_handoff(h))
                await step(w.coord, "target_db", lambda: w.coord.get_camilla_target_db())
    # apply_active_source_transition: the four carrier combos, ok / failed push.
    for prev, cur in (
        (Source.IDLE, Source.SPOTIFY),
        (Source.AIRPLAY, Source.BLUETOOTH),
        (Source.SPOTIFY, Source.IDLE),
        (Source.BLUETOOTH, Source.AIRPLAY),
        (Source.SPOTIFY, Source.BLUETOOTH),
        (Source.IDLE, Source.AIRPLAY),
        (Source.USBSINK, Source.USBSINK),
    ):
        for variant in ("ok", "push_fail", "push_fail_offline", "muted_latch", "remote_twist"):
            w = World(
                f"transition:{prev.value}>{cur.value}:{variant}", active={}, selected=cur.value,
                db=-20.0, level=45,
            )
            if variant.startswith("push_fail"):
                FX.push_ok = {"spotify": False, "bluetooth": False}
            if variant == "push_fail_offline":
                FX.offline = True
            if variant == "muted_latch":
                w.other_writer().save_mute_state(45, None)
            if variant == "remote_twist":
                w.other_writer().save_listening_level(80)
            await step(w.coord, "transition", lambda: w.coord.apply_active_source_transition(prev, cur))
            await step(w.coord, "state", lambda: w.coord.get_volume_state())
    # Deferred by a voice session; dropped when the lease disagrees.
    w = World("transition:voice_session", active={}, selected="spotify", level=50)
    w.coord.note_voice_session(True)
    await step(w.coord, "deferred", lambda: w.coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY))
    w.coord.note_voice_session(False)
    await step(w.coord, "applied", lambda: w.coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY))
    w = World("transition:dropped_at_lease", active={}, selected="spotify", level=50)
    w.backend.selected_script = ["airplay"]
    await step(w.coord, "dropped", lambda: w.coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY))


async def s_reconcile() -> None:
    from jasper.volume_curve import percent_to_db
    # Drift classes.
    for name, db, muted, level, pre_mute in (
        ("dead_band", percent_to_db(70) - 0.3, False, 70, None),
        ("boundary_minus", percent_to_db(70) - 1.0, False, 70, None),
        ("boundary_plus", percent_to_db(70) + 1.0, False, 70, None),
        ("quiet_drift", -18.0, False, 76, None),
        ("loud_drift", -8.0, False, 70, None),
        ("deep_loud", 0.0, False, 0, None),
        ("deep_quiet", percent_to_db(70) - 25.0, False, 70, None),
        ("zero_mute_drift", "floor", False, 0, None),
        ("toggle_latch", "floor", True, 59, 59),
        ("latch_unmuted_camilla", "floor", False, 59, 59),
        ("muted_at_level", "level", True, 50, None),
    ):
        w = World(
            f"reconcile:{name}", active={},
            db=percent_to_db(0) if db == "floor" else (percent_to_db(level) if db == "level" else db),
            muted=muted, level=level, mark_user_change=True,
        )
        if pre_mute is not None:
            w.other_writer().save_mute_state(pre_mute, None)
        for i in range(2):
            await step(w.coord, f"tick{i}", lambda: w.coord.maybe_reconcile_camilla())
            await step(w.coord, "deferred?", lambda: w.coord.reconcile_deferred)
    # Gates.
    for gate in ("voice_session", "voice_session_unlocked", "measurement", "offline", "push_source", "source_arg_push", "source_arg_idle"):
        w = World(
            f"reconcile:gate:{gate}", active={"spotactive": True} if gate == "push_source" else {},
            db=0.0, level=70, mark_user_change=True,
        )
        if gate == "voice_session":
            w.coord.note_voice_session(True)
        if gate == "voice_session_unlocked":
            w.coord.note_voice_session(True, camilla_volume_locked=False)
        if gate == "measurement":
            await step(w.coord, "measure_on", lambda: w.coord.note_measurement_active(True))
        if gate == "offline":
            FX.offline = True
        source = None
        if gate == "source_arg_push":
            source = Source.SPOTIFY
        if gate == "source_arg_idle":
            source = Source.IDLE
        await step(w.coord, "tick", lambda: w.coord.maybe_reconcile_camilla(source=source))
    # Cross-process level refresh on every tick (push source too).
    for source, selected in ((Source.SPOTIFY, "spotify"), (Source.IDLE, "idle")):
        w = World(f"reconcile:refresh:{source.value}", selected=selected, level=40)
        w.other_writer().save_listening_level(70, mark_user_change=True)
        await step(w.coord, "tick", lambda: w.coord.maybe_reconcile_camilla(source=source))
        await step(w.coord, "listening", lambda: w.coord.get_listening_level())
    # The DSP-writer lock: stand down once per episode, then correct.
    w = World("reconcile:dsp_writer_lock", active={}, db=0.0, level=40, mark_user_change=True)
    with advisory_file_lock(w.lock_path):
        for i in range(3):
            await step(w.coord, f"locked_tick{i}", lambda: w.coord.maybe_reconcile_camilla())
            await step(w.coord, "deferred?", lambda: w.coord.reconcile_deferred)
    await step(w.coord, "unlocked_tick", lambda: w.coord.maybe_reconcile_camilla())
    await step(w.coord, "deferred?", lambda: w.coord.reconcile_deferred)
    # The flock probe raises: fail open, warn once, say when it recovers.
    w = World("reconcile:probe_raises", active={}, db=0.0, level=40, mark_user_change=True)
    FX.graph_probe = "raises"
    for i in range(2):
        w.client.db = 0.0
        await step(w.coord, f"tick{i}", lambda: w.coord.maybe_reconcile_camilla())
    FX.graph_probe = "real"
    w.client.db = 0.0
    await step(w.coord, "recovered_tick", lambda: w.coord.maybe_reconcile_camilla())
    # The measurement hold: raises wait, lowerings never do.
    for hold in ("held", "unreadable", "malformed", "free"):
        for direction, db in (("raise", percent_to_db(70) - 25.0), ("lower", 0.0), ("unmute", "level_muted")):
            w = World(
                f"reconcile:hold:{hold}:{direction}", active={},
                db=percent_to_db(70) if db == "level_muted" else db,
                muted=db == "level_muted", level=70, mark_user_change=True,
            )
            FX.hold = hold
            for i in range(2):
                await step(w.coord, f"tick{i}", lambda: w.coord.maybe_reconcile_camilla())
                await step(w.coord, "deferred?", lambda: w.coord.reconcile_deferred)
            FX.hold = "free"
            await step(w.coord, "freed_tick", lambda: w.coord.maybe_reconcile_camilla())
    # A writer admitted while the hold is read still defers the raise.
    w = World("reconcile:writer_admitted_during_hold_read", active={}, db=percent_to_db(70) - 25.0, level=70, mark_user_change=True)
    held = ExitStack()
    real_get = control_client.get_measurement

    def read_while_writer_admitted(**kwargs):
        held.enter_context(advisory_file_lock(w.lock_path))
        return real_get(**kwargs)

    control_client.get_measurement = read_while_writer_admitted
    with held:
        await step(w.coord, "tick", lambda: w.coord.maybe_reconcile_camilla())
    control_client.get_measurement = real_get
    await step(w.coord, "after_tick", lambda: w.coord.maybe_reconcile_camilla())
    # Write failures: refused, then raised, then recovered (one line each way).
    for mode in ("writes_fail", "writes_raise"):
        w = World(f"reconcile:{mode}", active={}, db=-18.0, level=76, mark_user_change=True)
        setattr(FX, mode, True)
        for i in range(3):
            await step(w.coord, f"failing_tick{i}", lambda: w.coord.maybe_reconcile_camilla())
        setattr(FX, mode, False)
        await step(w.coord, "recovered_tick", lambda: w.coord.maybe_reconcile_camilla())
        await step(w.coord, "converged_tick", lambda: w.coord.maybe_reconcile_camilla())
    # The stranded measurement flag lapses on the tick's own clock.
    w = World("reconcile:lapse", active={}, db=0.0, level=70, mark_user_change=True)
    await step(w.coord, "measure_on", lambda: w.coord.note_measurement_active(True))
    advance(MEASUREMENT_AUTOCLEAR_SEC - 1.0)
    await step(w.coord, "tick_inside", lambda: w.coord.maybe_reconcile_camilla())
    await step(w.coord, "renew", lambda: w.coord.note_measurement_active(True))
    advance(2.0)
    await step(w.coord, "tick_renewed", lambda: w.coord.maybe_reconcile_camilla())
    advance(MEASUREMENT_AUTOCLEAR_SEC - 2.0)
    await step(w.coord, "tick_at_bound", lambda: w.coord.maybe_reconcile_camilla())
    w.client.db = 0.0
    await step(w.coord, "tick_after", lambda: w.coord.maybe_reconcile_camilla())
    # MEASURE_PAUSE lands while a tick awaits its Camilla read.
    w = World("reconcile:pause_during_read", active={}, db=-3.15, level=70, mark_user_change=True)
    FX.block_read = 1
    tick = asyncio.create_task(w.coord.maybe_reconcile_camilla())
    await asyncio.wait_for(FX.read_started.wait(), timeout=10)
    await step(w.coord, "measure_on", lambda: w.coord.note_measurement_active(True))
    FX.release_read.set()
    await tick
    rec("race", "tick_done")
    await step(w.coord, "tick_after_pause", lambda: w.coord.maybe_reconcile_camilla())
    # MEASURE_PAUSE waits for an in-flight write.
    w = World("reconcile:pause_waits_for_write", active={}, db=-3.15, level=70, mark_user_change=True)
    FX.block_write = 1
    tick = asyncio.create_task(w.coord.maybe_reconcile_camilla())
    await asyncio.wait_for(FX.write_started.wait(), timeout=10)
    pause = asyncio.create_task(w.coord.note_measurement_active(True))
    await asyncio.sleep(0)
    rec("race", "pause_pending", pause.done())
    FX.release_write.set()
    await tick
    await pause
    rec("race", "pause_done")
    w.client.db = 0.0
    await step(w.coord, "tick_paused", lambda: w.coord.maybe_reconcile_camilla())
    # A stale preflight cannot overwrite a newer cross-daemon command.
    w = World("reconcile:stale_preflight", active={}, db=0.0, level=60, mark_user_change=True)
    control = w.make_coord()
    FX.block_read = 1
    tick = asyncio.create_task(w.coord.maybe_reconcile_camilla())
    await asyncio.wait_for(FX.read_started.wait(), timeout=10)
    await step(control, "control_set:20", lambda: control.set_listening_level(20))
    FX.release_read.set()
    await tick
    rec("race", "tick_done")
    await step(w.coord, "listening", lambda: w.coord.get_listening_level())
    # The household write is a claim a measurement claim outranks.
    from jasper.volume_owner import ClaimKind
    w = World("reconcile:measurement_claim", active={}, db=percent_to_db(40), level=40)
    await step(w.coord, "seed_tick", lambda: w.coord.maybe_reconcile_camilla())
    claim = await step(w.coord, "acquire_measurement", lambda: w.coord.volume_owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, -12.5))
    await step(w.coord, "set:70", lambda: w.coord.set_listening_level(70))
    await step(w.coord, "tick", lambda: w.coord.maybe_reconcile_camilla())
    if claim is not None:
        await step(w.coord, "release", lambda: w.coord.volume_owner.release(claim))
    duck = await step(w.coord, "acquire_duck", lambda: w.coord.volume_owner.acquire_duck(5.0))
    await step(w.coord, "tick_under_duck", lambda: w.coord.maybe_reconcile_camilla())
    if duck is not None:
        await step(w.coord, "release_duck", lambda: w.coord.volume_owner.release(duck))


async def s_duck_lock() -> None:
    w = World("duck:voice_session_locked", active={}, db=-25.0, level=50)
    w.coord.note_voice_session(True)
    await steps(w.coord, [
        ("set:46", lambda: w.coord.set_listening_level(46)),
        ("set:0", lambda: w.coord.set_listening_level(0)),
        ("adjust:+10", lambda: w.coord.adjust_listening_level(10)),
        ("mute", lambda: w.coord.mute()),
        ("unmute", lambda: w.coord.unmute()),
        ("target_db", lambda: w.coord.get_camilla_target_db()),
        ("context", lambda: w.coord.effective_volume_context()),
        ("transition", lambda: w.coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)),
        ("tick", lambda: w.coord.maybe_reconcile_camilla()),
    ])
    w.coord.note_voice_session(False)
    await step(w.coord, "set_after:50", lambda: w.coord.set_listening_level(50))
    w = World("duck:voice_session_unlocked", active={}, db=-25.0, level=50)
    w.coord.note_voice_session(True, camilla_volume_locked=False)
    await steps(w.coord, [
        ("set:46", lambda: w.coord.set_listening_level(46)),
        ("transition", lambda: w.coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)),
        ("context", lambda: w.coord.effective_volume_context()),
    ])
    for answer in (True, False, None, "raises"):
        for active, selected in (({}, None), ({"spotactive": True}, None), ({"usbsinkactive": True}, None)):
            w = World(
                f"duck:probe={answer}:{sorted(active)}", active=active, selected=selected,
                db=-40.0, level=64, duck=True,
            )
            FX.duck = answer
            await steps(w.coord, [
                ("set:70", lambda: w.coord.set_listening_level(70)),
                ("adjust:+12", lambda: w.coord.adjust_listening_level(12)),
                ("set:0", lambda: w.coord.set_listening_level(0)),
                ("mute", lambda: w.coord.mute()),
                ("unmute", lambda: w.coord.unmute()),
                ("observe:30", lambda: w.coord.observe_source_volume(Source.USBSINK, 30)),
                ("prepare", lambda: w.coord.prepare_source_handoff(Source.SPOTIFY, Source.AIRPLAY, reason="duck")),
                ("target_db", lambda: w.coord.get_camilla_target_db()),
            ])


async def s_boundaries() -> None:
    from jasper.volume_curve import percent_to_db
    # already_safe: a ducked fader exactly 1 dB above the guard is safe.
    for offset in (1.0, 1.01, 0.0):
        w = World(
            f"boundary:duck_already_safe:+{offset}", active={"spotactive": True},
            db=percent_to_db(60) + offset, level=60, duck=True,
        )
        FX.duck = True
        FX.push_ok["spotify"] = False
        await step(w.coord, "set:60", lambda: w.coord.set_listening_level(60))
    # The handoff guard: a fader exactly 1 dB above the guard needs none.
    for offset in (1.0, 1.01):
        w = World(
            f"boundary:handoff_guard:+{offset}", active={}, selected="airplay",
            db=percent_to_db(50) + offset, level=50,
        )
        h = await step(w.coord, "prepare", lambda: w.coord.prepare_source_handoff(Source.SPOTIFY, Source.AIRPLAY, reason="boundary"))
        if h is not None:
            await step(w.coord, "finalize", lambda: w.coord.finalize_source_handoff(h))
    # The reconciler's in-lease re-read lands exactly on the dead band.
    for offset in (1.0, -1.0, 1.01):
        w = World(f"boundary:reconcile_in_lease:{offset}", active={}, db=0.0, level=50, mark_user_change=True)
        FX.read_values = [0.0, percent_to_db(50) + offset]
        await step(w.coord, "tick", lambda: w.coord.maybe_reconcile_camilla())
    # The push-carrier target around the guard threshold.
    for selected in ("spotify", "idle"):
        for level in (70, 0):
            for persisted in (0.0, -1.0, -1.01, -25.0, 1.0):
                w = World(
                    f"boundary:target:{selected}:{level}:{persisted}", active={}, selected=selected,
                    db=-3.0, level=level,
                )
                w.other_writer().save_now(persisted)
                await step(w.coord, "target_db", lambda: w.coord.get_camilla_target_db())
                FX.offline = True
                await step(w.coord, "context_offline", lambda: w.coord.effective_volume_context())
                FX.offline = False


async def s_offline() -> None:
    for name, active in (("idle", {}), ("spotify", {"spotactive": True}), ("usbsink", {"usbsinkactive": True})):
        w = World(f"offline:{name}", active=active, db=-20.0, level=50)
        FX.offline = True
        await steps(w.coord, [
            ("initialize", lambda: w.coord.initialize()),
            ("set:70", lambda: w.coord.set_listening_level(70)),
            ("set:0", lambda: w.coord.set_listening_level(0)),
            ("mute", lambda: w.coord.mute()),
            ("unmute", lambda: w.coord.unmute()),
            ("observe:usbsink:30", lambda: w.coord.observe_source_volume(Source.USBSINK, 30)),
            ("target_db", lambda: w.coord.get_camilla_target_db()),
            ("context", lambda: w.coord.effective_volume_context()),
            ("tick", lambda: w.coord.maybe_reconcile_camilla()),
            ("prepare", lambda: w.coord.prepare_source_handoff(Source.SPOTIFY, Source.AIRPLAY, reason="offline")),
        ])
        FX.offline = False
        await step(w.coord, "online_set:40", lambda: w.coord.set_listening_level(40))
    # A context snapshot that never settles degrades after three attempts.
    w = World("offline:context_churn", active={}, db=-20.0, level=50)
    churn = itertools.count(51)
    other = w.other_writer()
    FX.on_read = lambda: other.save_listening_level(next(churn))
    await step(w.coord, "context", lambda: w.coord.effective_volume_context())
    FX.on_read = None
    for pre_mute in (None, 59):
        w = World(f"offline:context_unreadable:pre_mute={pre_mute}", active={}, db=-20.0, level=59)
        if pre_mute is not None:
            w.other_writer().save_mute_state(pre_mute, None)
        await step(w.coord, "context_readable", lambda: w.coord.effective_volume_context())
        FX.offline = True
        await step(w.coord, "context_unreadable", lambda: w.coord.effective_volume_context())


def _record(level=None, *, db=None, age_s=0.0, pre_mute=None, token=None, v1=False):
    stamp = (WALL_EPOCH - timedelta(seconds=age_s)).isoformat(timespec="seconds").replace("+00:00", "Z")
    body = {"main_volume_db": db, "updated_at": stamp}
    if v1:
        return body
    body["version"] = 2
    if level is not None:
        body["listening_level"] = level
    body["last_used_at"] = stamp
    if pre_mute is not None:
        body["pre_mute_level"] = pre_mute
    if token is not None:
        body["mute_token"] = token
    return body


async def s_boot() -> None:
    from jasper.volume_curve import percent_to_db
    records = {
        "first": None,
        "fresh": _record(80, db=percent_to_db(80), age_s=60.0),
        "stale_high": _record(90, db=percent_to_db(90), age_s=7200.0),
        "stale_low": _record(10, db=percent_to_db(10), age_s=7200.0),
        "stale_in_band": _record(50, db=percent_to_db(50), age_s=7200.0),
        "fresh_zero": _record(0, db=percent_to_db(0), age_s=60.0),
        "muted_latch": _record(60, db=percent_to_db(0), age_s=60.0, pre_mute=60, token="boot-token"),
        "v1": _record(db=-20.0, v1=True, age_s=60.0),
        "corrupt_level": _record(250, db=-10.0, age_s=60.0),
    }
    for name, record in records.items():
        for source_name, active in (("idle", {}), ("spotify", {"spotactive": True}), ("airplay", {"aplactive": True})):
            w = World(f"boot:{name}:{source_name}", active=active, db=-30.0, record=record)
            await step(w.coord, "initialize", lambda: w.coord.initialize())
            await step(w.coord, "state", lambda: w.coord.get_volume_state())
    w = World("boot:custom_bounds", active={}, record=_record(95, db=-2.0, age_s=100.0))
    await step(w.coord, "initialize", lambda: w.coord.initialize(stale_after_sec=60.0, safe_low_pct=30, safe_high_pct=60, first_boot_default_pct=42))


async def s_plus_one_db() -> None:
    """A persisted +1.0 dB main_volume_db (load accepts up to ceiling + 1 dB)."""
    before = len([e for e in STREAM if e[0] == "log" and "camilla.main_volume_clamped" in e[2]])
    for source_name, active in (("idle", {}), ("spotify", {"spotactive": True}), ("usbsink", {"usbsinkactive": True})):
        w = World(f"plus_one_db:{source_name}", active=active, db=0.0, record=_record(80, db=1.0, age_s=60.0))
        await steps(w.coord, [
            ("target_db", lambda: w.coord.get_camilla_target_db()),
            ("context", lambda: w.coord.effective_volume_context()),
            ("observe:equal", lambda: w.coord.observe_source_volume(Source.SPOTIFY, 80)),
            ("tick", lambda: w.coord.maybe_reconcile_camilla()),
            ("prepare", lambda: w.coord.prepare_source_handoff(Source.SPOTIFY, Source.AIRPLAY, reason="plus_one")),
        ])
        w2 = World(f"plus_one_db:{source_name}:offline_context", active=active, db=0.0, record=_record(80, db=1.0, age_s=60.0))
        FX.offline = True
        await step(w2.coord, "context_offline", lambda: w2.coord.effective_volume_context())
        await step(w2.coord, "target_db_offline", lambda: w2.coord.get_camilla_target_db())
        FX.offline = False
        w3 = World(f"plus_one_db:{source_name}:push_fail", active=active, db=0.0, record=_record(80, db=1.0, age_s=60.0))
        FX.push_ok = {"spotify": False, "bluetooth": False}
        await step(w3.coord, "set:80", lambda: w3.coord.set_listening_level(80))
        await step(w3.coord, "initialize", lambda: w3.coord.initialize())
    after = len([e for e in STREAM if e[0] == "log" and "camilla.main_volume_clamped" in e[2]])
    rec("plus_one_db_clamp_lines", after - before)


async def s_measuring() -> None:
    w = World("measuring:doors", active={}, db=-20.0, level=60)
    await step(w.coord, "measure_on", lambda: w.coord.note_measurement_active(True))
    await steps(w.coord, [
        ("set:95", lambda: w.coord.set_listening_level(95)),
        ("set:25", lambda: w.coord.set_listening_level(25)),
        ("adjust:+35", lambda: w.coord.adjust_listening_level(35)),
        ("adjust:-35", lambda: w.coord.adjust_listening_level(-35)),
        ("unmute_unlatched", lambda: w.coord.unmute()),
        ("mute", lambda: w.coord.mute()),
        ("unmute", lambda: w.coord.unmute()),
        ("set_muted:False", lambda: w.coord.set_muted(False)),
        ("toggle_from_muted", lambda: w.coord.toggle_mute()),
        ("set_muted:True", lambda: w.coord.set_muted(True)),
        ("state", lambda: w.coord.get_volume_state()),
    ])
    await step(w.coord, "measure_off", lambda: w.coord.note_measurement_active(False))
    await step(w.coord, "toggle_after", lambda: w.coord.toggle_mute())
    w = World("measuring:toggle_to_muted", active={}, db=-20.0, level=60)
    await step(w.coord, "measure_on", lambda: w.coord.note_measurement_active(True))
    await step(w.coord, "toggle_to_muted", lambda: w.coord.toggle_mute())
    # The lapse: exactly at the bound, once per window, per window.
    w = World("measuring:lapse", active={}, db=0.0, level=70, mark_user_change=True)
    await step(w.coord, "measure_on", lambda: w.coord.note_measurement_active(True))
    advance(MEASUREMENT_AUTOCLEAR_SEC - 0.5)
    await step(w.coord, "set:20_inside", lambda: w.coord.set_listening_level(20))
    advance(0.5)
    await step(w.coord, "set:20_at_bound", lambda: w.coord.set_listening_level(20))
    await step(w.coord, "adjust:-5", lambda: w.coord.adjust_listening_level(-5))
    await step(w.coord, "observe_while_lapsed", lambda: w.coord.observe_source_volume(Source.IDLE, 5))
    await step(w.coord, "measure_on_again", lambda: w.coord.note_measurement_active(True))
    await step(w.coord, "set:30_inside", lambda: w.coord.set_listening_level(30))
    advance(MEASUREMENT_AUTOCLEAR_SEC)
    await step(w.coord, "set:30_lapsed", lambda: w.coord.set_listening_level(30))
    await step(w.coord, "unmute_lapsed", lambda: w.coord.unmute())
    await step(w.coord, "tick_clears", lambda: w.coord.maybe_reconcile_camilla())
    await step(w.coord, "set:31", lambda: w.coord.set_listening_level(31))
    await step(w.coord, "measure_off", lambda: w.coord.note_measurement_active(False))
    await step(w.coord, "set:32", lambda: w.coord.set_listening_level(32))


async def s_diagnostics() -> None:
    """``/state.audio.volume_policy`` shares the guard check with the handoff."""
    from jasper.volume_diagnostics import build_volume_policy_snapshot
    rec("scenario", "diagnostics")
    for active_source in (None, "spotify", "airplay", "bluetooth", "usbsink", "idle", "bogus"):
        for main_db in (None, 0.0, -0.5, -1.0, -1.01, -25.0):
            for persisted_db in (None, 0.0, -1.0, -1.01, -32.5, 1.0):
                for mux in (None, {"selected_source": "bluetooth", "last_handoff": {"result": "ok"}}, {"winner": "spotify"}):
                    rec("policy", build_volume_policy_snapshot(
                        active_source=active_source,
                        listening_level=42,
                        main_volume_db=main_db,
                        persisted_main_volume_db=persisted_db,
                        mux_status=mux,
                    ))


SCENARIOS = [
    s_verbs, s_observe, s_handoff, s_reconcile, s_duck_lock, s_boundaries,
    s_offline, s_boot, s_plus_one_db, s_measuring, s_diagnostics,
]


async def _run() -> None:
    for scenario in SCENARIOS:
        await scenario()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump")
    parser.add_argument("--names")
    parser.add_argument("--expect-root")
    args = parser.parse_args()
    root = Path(jasper.__file__).resolve().parent.parent
    if args.expect_root and root != Path(args.expect_root).resolve():
        print(f"jasper imported from {root}, not {args.expect_root}", file=sys.stderr)
        return 2
    with asyncio.Runner(loop_factory=_RealClockLoop) as runner:
        runner.run(_run())
    blob = json.dumps(STREAM, sort_keys=True)
    digest = hashlib.sha256(blob.encode()).hexdigest()
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            for entry in STREAM:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
    if args.names:
        with open(args.names, "w", encoding="utf-8") as fh:
            for key in sorted(NAMES):
                fh.write(f"{sorted(NAMES[key])}\t{key}\n")
    counts: dict[str, int] = {}
    for entry in STREAM:
        counts[entry[0]] = counts.get(entry[0], 0) + 1
    clamp = [e for e in STREAM if e[0] == "plus_one_db_clamp_lines"]
    print(f"root={root}")
    print(f"records={len(STREAM)} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"plus_one_db_clamp_lines={clamp[0][1] if clamp else 'missing'}")
    print(f"digest={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
