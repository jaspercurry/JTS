# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Integration and contract tests for the household sound setup surface."""

from __future__ import annotations

from jasper import output_topology_store as topology_mod
from jasper.active_speaker.candidate_bank import bank_candidate

import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call
from tests.test_rear_preview import compare_evidence as compare_evidence

import numpy as np
import pytest

from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS, programs_for_topology
from jasper.active_speaker import playback_route
from jasper.active_speaker.calibration_level import (
    load_calibration_level_state,
    update_calibration_level_state,
)
from jasper.active_speaker.safe_playback import load_safe_playback_state
from jasper.active_speaker.commissioning_coordinator import build_commissioning_view
from jasper.active_speaker.baseline_profile import persist_applied_baseline_profile
from jasper.active_speaker.design_draft import declared_driver_spacing_m, load_design_draft
from jasper.active_speaker.tuning_handoff import build_tuning_handoff
from jasper.audio_measurement.program_analysis.model import MeasurementGeometry
from jasper.active_speaker.runtime_convergence import PARK_SKIPPED, park_and_commit_topology
from jasper.active_speaker.runtime_contract import (
    FLAT_PROGRAM_GRAPH_UNCONFIGURED,
    PARKED_MUTED_STATUS,
    apply_safe_graph_decision_to_statefile,
    parked_safe_graph_decision,
    safe_graph_for_current_topology,
)
from jasper.audio_hardware.dac import all_profiles as dac_all_profiles
from jasper.camilla_config_contract import PeqFilter
from jasper.dsp_apply import DspApplyState, dsp_write_epoch, record_dsp_apply_state
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    write_state as write_output_hardware_state,
)
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import (
    ParametricBand,
    SimpleEq,
    SoundProfile,
    load_profile,
    loudness_compensation_db,
    load_profile_library,
    save_profile,
)
from jasper.sound.runtime import (
    _config_without_id_header,
    reconcile_current_dsp,
)
from jasper.sound.settings import (
    DEFAULT_VOLUME_FLOOR_DB,
    SoundSettings,
    load_sound_settings,
)
from jasper.volume_curve import percent_to_db
from jasper.web import (
    _common,
    nav,
    sound_active_speaker,
    sound_profile_apply,
    sound_setup,
    volume_floor_tone,
)

from .active_speaker_fixtures import (
    PASSIVE_ONLY_DAC_ID,
    PASSIVE_ONLY_DAC_LABEL,
    register_passive_only_dac,
    mono_output_topology,
)
from ._hat_eeprom import write_hat_eeprom
from ._log_events import event_records, parse_event
from ._web_test_helpers import (
    json_post_with_csrf,
    make_csrf_session,
    request_with_csrf,
)
from jasper.output_topology import (
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    OutputTopologyError,
)
from jasper.output_topology_store import new_topology_draft
from jasper.output_topology_store import (
    OutputTopologyMutation,
    load_output_topology,
    output_topology_mutation,
    save_output_topology,
)
from .sound_camilla_fixtures import FakeCamilla
from .test_active_speaker_runtime_contract import _active_baseline_yaml


class _RuntimeStep:
    ok = True
    live_applied = True
    error = None

    @staticmethod
    def to_dict():
        return {"ok": True, "live_applied": True}


class _RuntimeMutation:
    def __init__(self, committed_topology: OutputTopology) -> None:
        self.parked = _RuntimeStep()
        self.convergence = _RuntimeStep()
        self.committed_topology = committed_topology
        self.prior_config_path = "/tmp/prior.yml"


def _commit_topology_runtime(_topology, commit, **_kwargs):
    from jasper.active_speaker.runtime_convergence import OUTPUTD_UNIT
    from jasper.control.restart_broker import manage_units

    stopped = manage_units(
        OUTPUTD_UNIT,
        verb="stop",
        reason="output topology replace",
        no_block=False,
        timeout=15.0,
    )
    if not stopped.get("ok"):
        raise RuntimeError(str(stopped.get("error") or "could not stop outputd"))
    return _RuntimeMutation(commit())


@pytest.fixture(autouse=True)
def _no_privileged_unit_actions(monkeypatch, tmp_path: Path):
    """Keep the suite from asking systemd to start units.

    Saving an output topology kicks ``jasper-audio-hardware-reconcile`` through
    the restart broker, so every topology-save test would otherwise reach for a
    privileged action (and would really run ``systemctl`` if the suite is run as
    root). Tests that assert on the broker contract re-patch this in their own
    body, which wins because it is applied after the fixture.
    """
    def fake_manage_units(*units, **_kwargs):
        if units == ("jasper-outputd.service",):
            return {"ok": True}
        return {"ok": False, "error": "stubbed in tests"}

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        fake_manage_units,
    )
    # Topology saves now stop the active-speaker safety session before parking.
    # Keep that real state transition inside each test's isolated writable
    # directory, as the deployed route does through its configured state path.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe_playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        _commit_topology_runtime,
    )


def _event_record(caplog, event: str):
    """The single event record and its fields."""

    (record,) = event_records(caplog, event)
    parsed = parse_event(record.getMessage())
    assert parsed is not None
    return record, parsed[1]


def _stub_audio_stops(monkeypatch, stops: list[str] | None = None) -> list[str]:
    recorded = [] if stops is None else stops

    def stop_safe() -> dict:
        recorded.append("safe")
        return {"status": "idle"}

    monkeypatch.setattr(sound_active_speaker, "_active_speaker_stop_payload", stop_safe)
    return recorded


def _follower_post_status(base: str, path: str, session: dict) -> int:
    """POST an empty JSON body to ``path`` and return the HTTP status code.

    Unlike ``json_post_with_csrf`` (which asserts an exact status), this returns
    the code so a test can assert on the follower gate alone — whether the route
    was blocked (409) vs reached its handler (200/502) — independent of backend
    state the active-speaker handlers touch."""
    req = urllib.request.Request(
        base + path,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": session["token"],
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(session["jar"]),
    )
    try:
        return opener.open(req).status
    except urllib.error.HTTPError as e:
        return e.code


def _follower_get_status(base: str, path: str, session: dict) -> int:
    """GET ``path`` (no follower gate exists for GETs) and return the status."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(session["jar"]),
    )
    try:
        return opener.open(base + path).status
    except urllib.error.HTTPError as e:
        return e.code


def _room_config(peqs: list[PeqFilter] | None = None) -> str:
    return emit_sound_config(
        SoundProfile(enabled=False),
        room_peqs=peqs or [],
    )


def _preamp_gain(config_text: str) -> float:
    """The broadband output-trim filter's gain, out of the emitted graph."""

    import yaml

    return yaml.safe_load(config_text)["filters"]["sound_preamp"]["parameters"]["gain"]


def _configure_passive_layout_for_eq(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Give EQ tests a safe, explicit nonzero speaker layout.

    The production contract parks a speaker with no saved groups. These tests
    exercise EQ composition, so they must model the separate case where a
    passive full-range speaker has already been configured.
    """

    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "eq_passive",
        "name": "EQ passive speaker",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [{
            "id": "main",
            "label": "Main speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{
                "role": "full_range",
                "physical_output_index": 0,
                "identity_verified": True,
            }],
        }],
        "routing": {"mono_group_id": "main"},
    }), path=topology_path)


def _record_dsp_epoch(path: Path, op_id: str) -> None:
    record_dsp_apply_state(
        DspApplyState(
            schema_version=1,
            op_id=op_id,
            source="test",
            phase="done",
            result="success",
            started_at="2026-05-28T00:00:00Z",
            finished_at="2026-05-28T00:00:01Z",
            prior_config_path=None,
            candidate_config_path="/tmp/test.yml",
        ),
        state_path=path,
    )


class FakeCamillaWithoutLiveRaw:
    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None
        self.set_calls: list[str] = []

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.set_calls.append(path)
        self.loaded_path = path
        return True


class FakeVolumeCamilla:
    def __init__(self, db: float = -18.0, muted: bool = True) -> None:
        self.db = db
        self.muted = muted
        self.events: list[tuple[str, float | bool, bool]] = []

    async def get_volume_and_mute(
        self, *, best_effort: bool = False,
    ) -> tuple[float, bool]:
        return self.db, self.muted

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        self.events.append(("volume", db, best_effort))
        self.db = db
        return True

    async def set_main_mute(
        self, muted: bool, *, best_effort: bool = False,
    ) -> bool:
        self.events.append(("mute", muted, best_effort))
        self.muted = muted
        return True


def _install_floor_tone_owner(fake: FakeVolumeCamilla) -> None:
    """Bind the process fader owner to this fake, as `jasper.web` binds the real one.

    The audition's level control is a COMMISSIONING claim, so a test with no
    owner registered exercises the degrade path instead of the subject. The
    autouse `_isolate_process_volume_owner` fixture clears this again after
    each test.
    """
    from jasper.volume_owner import VolumeOwner, install_volume_owner

    async def _read() -> float:
        return fake.db

    async def _write(db: float) -> bool:
        # best_effort=True is the owner's door contract in BOTH directions: an
        # unconfirmable level refuses the claim outright rather than relying on
        # the setter to raise. `jasper.web` binds the real door the same way.
        return await fake.set_volume_db(db, best_effort=True)

    install_volume_owner(
        VolumeOwner(set_fader_db=_write, get_fader_db=_read)
    )


class BlockingVolumeCamilla(FakeVolumeCamilla):
    def __init__(
        self,
        *,
        db: float = -18.0,
        muted: bool = True,
        block_on_volume_call: int,
    ) -> None:
        super().__init__(db=db, muted=muted)
        self.block_on_volume_call = block_on_volume_call
        self.volume_calls = 0
        self.volume_call_entered = asyncio.Event()
        self.release_volume_call = asyncio.Event()

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        self.volume_calls += 1
        self.events.append(("volume", db, best_effort))
        if self.volume_calls == self.block_on_volume_call:
            self.volume_call_entered.set()
            await self.release_volume_call.wait()
        self.db = db
        return True


class FakeVolumeFloorToneRunner:
    instances: list["FakeVolumeFloorToneRunner"] = []

    def __init__(self, wav_path: Path, *, on_finish=None) -> None:
        self.wav_path = wav_path
        self.on_finish = on_finish
        self.started = False
        self.stopped = False
        self.error: str | None = None
        FakeVolumeFloorToneRunner.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    @property
    def running(self) -> bool:
        return self.started and not self.stopped and self.error is None


_SOUND_MODULE = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "sound-profile" / "js" / "main.js"
)
_SOUND_HARNESS = Path(__file__).resolve().parent / "js" / "sound_profile_harness.mjs"

_NODE = shutil.which("node")


@contextmanager
def sound_server(tmp_path: Path):
    """Serve the real ``/sound/`` handler on loopback; yield its base URL."""

    try:
        server = sound_setup.make_server(
            ("127.0.0.1", 0),
            profile_path=tmp_path / "sound_profile.json",
            library_path=tmp_path / "sound_profiles.json",
            config_dir=tmp_path / "configs",
        )
    except PermissionError:
        pytest.skip("environment does not allow loopback test server bind")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _ReadTrackingBytesIO(io.BytesIO):
    def __init__(self, initial_bytes: bytes) -> None:
        super().__init__(initial_bytes)
        self.read_calls: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        return super().read(size)


class _BrokenPipeBytesIO(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.write_calls = 0

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        raise BrokenPipeError("synthetic client disconnect")


def _drive_raw_sound_post(
    tmp_path: Path,
    *,
    path: str,
    content_length: int,
    body: bytes = b"must-not-be-read",
    response_sink: io.BytesIO | None = None,
) -> tuple[bytes, list[int]]:
    """Drive the real sound Handler with an otherwise-valid raw POST."""

    handler_cls = sound_setup._make_handler(
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "sound_profiles.json",
        config_dir=tmp_path / "configs",
        camilla_factory=lambda: None,
    )
    raw = (
        f"POST {path} HTTP/1.1\r\n".encode()
        + b"Host: jts.local\r\n"
        + f"Content-Length: {content_length}\r\n".encode()
        + b"\r\n"
        + body
    )
    rfile = _ReadTrackingBytesIO(raw)
    wfile = response_sink if response_sink is not None else io.BytesIO()
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = rfile
    handler.wfile = wfile
    handler.client_address = ("127.0.0.1", 0)
    handler.server = None
    handler.raw_requestline = rfile.readline()
    assert handler.parse_request() is True
    handler.protocol_version = "HTTP/1.1"
    handler.do_POST()
    return wfile.getvalue(), rfile.read_calls


@pytest.mark.parametrize(
    ("method", "route", "builder", "event", "extra_fields"),
    [
        (
            "GET",
            "/output-topology",
            "_output_topology_payload",
            "sound.output_topology",
            {},
        ),
        (
            "GET",
            "/active-speaker/tuning-handoff",
            "_active_speaker_tuning_handoff_payload",
            "sound.active_speaker_tuning_handoff",
            {},
        ),
    ],
)
def test_sound_route_builder_failure_answers_502_and_logs_one_error_event(
    tmp_path,
    monkeypatch,
    caplog,
    method,
    route,
    builder,
    event,
    extra_fields,
):
    """A failed route answers 502 and records its exception exactly once."""
    error = OSError("payload builder failed")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(sound_setup, builder, fail)
    caplog.set_level(logging.ERROR, logger=sound_setup.logger.name)
    with sound_server(tmp_path) as base:
        if method == "GET":
            try:
                urllib.request.urlopen(f"{base}{route}")
            except urllib.error.HTTPError as e:
                response = e
            else:
                raise AssertionError(f"{route} did not fail the request")
            assert response.code == 502
        else:
            response = json_post_with_csrf(base, route, {}, expect_status=502)
        assert response.headers.get_content_type() == "application/json"
        payload = json.loads(response.read().decode("utf-8"))

    assert payload == {"error": str(error)}
    record, fields = _event_record(caplog, event)
    assert fields == {"result": "error", **extra_fields}
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is error
    assert record.exc_info[2] is not None


@pytest.mark.parametrize(
    ("content_length", "expected_code"),
    [
        (-1, "negative_content_length"),
        (sound_setup.MAX_JSON_BYTES + 1, "body_too_large"),
    ],
)
def test_sound_post_rejects_invalid_body_length_before_read(
    tmp_path,
    monkeypatch,
    content_length,
    expected_code,
):
    monkeypatch.setattr(_common, "guard_mutating_request", lambda _handler: True)

    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/i2s-hat",
        content_length=content_length,
    )

    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["code"] == expected_code
    assert read_calls == []


def test_sound_post_unknown_route_precedes_csrf_and_body_read(tmp_path, monkeypatch):
    def fail_if_guarded(_handler):
        raise AssertionError("unknown route must return before the CSRF guard")

    monkeypatch.setattr(_common, "guard_mutating_request", fail_if_guarded)

    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/not-a-sound-route",
        content_length=-1,
    )

    assert b" 404 " in response.split(b"\r\n", 1)[0]
    assert read_calls == []


def test_dead_active_speaker_post_routes_are_unregistered(tmp_path):
    handler = sound_setup._make_handler(
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "sound_profiles.json",
        config_dir=tmp_path / "configs",
    )
    dispatch = handler.do_POST
    routes = dispatch.__closure__[
        dispatch.__code__.co_freevars.index("_POST_ROUTES")
    ].cell_contents
    dead = {
        "/active-speaker/crossover-preview",
        "/active-speaker/stop",
        "/active-speaker/channel-protection",
        "/active-speaker/stage-config",
        "/active-speaker/check-path-safety",
        "/active-speaker/load-startup-config",
        "/active-speaker/commission-load",
        "/active-speaker/commission-ramp-step",
        "/active-speaker/commission-ramp-ack",
        "/active-speaker/driver-measurement",
        "/active-speaker/summed-test",
        "/active-speaker/summed-test/level",
        "/active-speaker/summed-test/stop",
        "/active-speaker/summed-validation",
    }
    assert dead.isdisjoint(routes)


def test_seat_level_start_route_dispatches_and_is_csrf_protected(tmp_path, monkeypatch):
    """#2761: POST /active-speaker/seat-level/start reaches
    _seat_level_start_payload only after the CSRF chokepoint
    (guard_mutating_request, wired via dispatch_post(..., guard="header"))."""
    monkeypatch.setattr(_common, "guard_mutating_request", lambda _handler: False)
    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/active-speaker/seat-level/start",
        content_length=-1,
    )
    assert b" 403 " in response.split(b"\r\n", 1)[0]
    assert read_calls == []

    monkeypatch.setattr(_common, "guard_mutating_request", lambda _handler: True)
    monkeypatch.setattr(
        sound_setup,
        "_seat_level_start_payload",
        lambda body: {"route": "seat-level-start", "body": body},
    )
    body = b'{"target_db_spl": 78.0}'
    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/active-speaker/seat-level/start",
        content_length=len(body),
        body=body,
    )
    assert b" 200 " in response.split(b"\r\n", 1)[0]
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert payload == {
        "route": "seat-level-start",
        "body": {"target_db_spl": 78.0},
    }
    assert read_calls == [len(body)]


@pytest.mark.parametrize("path,builder,expected", [
    ("/active-speaker/seat-level/status", "_seat_level_status_payload", {
        "state": "idle", "target_db_spl": None,
        "mic": {"available": False}, "default_target_db_spl": 78.0,
    }),
])
def test_seat_level_state_routes(tmp_path, monkeypatch, path, builder, expected):
    monkeypatch.setattr(sound_setup, builder, lambda: expected)

    handler_cls = sound_setup._make_handler(
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "sound_profiles.json",
        config_dir=tmp_path / "configs",
        camilla_factory=lambda: None,
    )
    rfile = io.BytesIO(
        f"GET {path} HTTP/1.1\r\nHost: jts.local\r\n\r\n".encode()
    )
    wfile = io.BytesIO()
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = rfile
    handler.wfile = wfile
    handler.client_address = ("127.0.0.1", 0)
    handler.server = None
    handler.raw_requestline = rfile.readline()
    assert handler.parse_request() is True
    handler.protocol_version = "HTTP/1.1"
    handler.do_GET()
    response = wfile.getvalue()

    assert b" 200 " in response.split(b"\r\n", 1)[0]
    headers, body = response.split(b"\r\n\r\n", 1)
    assert b"Content-Type: application/json" in headers
    payload = json.loads(body)
    assert payload == expected


def test_seat_level_stop_route_dispatches_and_is_csrf_protected(tmp_path, monkeypatch):
    """#2761: POST /active-speaker/seat-level/stop reaches
    _seat_level_stop_payload only after the same CSRF chokepoint."""
    monkeypatch.setattr(_common, "guard_mutating_request", lambda _handler: False)
    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/active-speaker/seat-level/stop",
        content_length=-1,
    )
    assert b" 403 " in response.split(b"\r\n", 1)[0]
    assert read_calls == []

    monkeypatch.setattr(_common, "guard_mutating_request", lambda _handler: True)
    monkeypatch.setattr(
        sound_setup,
        "_seat_level_stop_payload",
        lambda: {"route": "seat-level-stop"},
    )
    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/active-speaker/seat-level/stop",
        content_length=2,
        body=b"{}",
    )
    assert b" 200 " in response.split(b"\r\n", 1)[0]
    assert json.loads(response.split(b"\r\n\r\n", 1)[1]) == {
        "route": "seat-level-stop",
    }
    assert read_calls == [2]


def test_sound_post_csrf_rejection_precedes_body_read(tmp_path, monkeypatch):
    guard_calls = []

    def reject(_handler):
        guard_calls.append("guard")
        return False

    monkeypatch.setattr(_common, "guard_mutating_request", reject)

    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/i2s-hat",
        content_length=-1,
    )

    assert b" 403 " in response.split(b"\r\n", 1)[0]
    assert guard_calls == ["guard"]
    assert read_calls == []

    monkeypatch.setattr(_common, "guard_mutating_request", lambda _handler: True)
    body = b'{"profile_id":1}'
    response, read_calls = _drive_raw_sound_post(
        tmp_path, path="/i2s-hat", content_length=len(body), body=body
    )
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert read_calls == [len(body)]


# The EQ chrome the hardware pages must not render: the Off/Saved/Draft
# tablist and the now-playing plot are content-DSP surfaces.
_EQ_ONLY_CHROME = ('id="tab-off"', 'id="tab-saved"', 'id="tab-draft"', 'id="plot"')


def _crossover_child_row() -> tuple[str, str]:
    """The nav row /sound/speaker/ links itself, as (relative href, label)."""
    row = nav.entry("/sound/speaker/crossover/")
    return row.path.removeprefix(row.parent), row.label


@pytest.mark.parametrize(
    ("page_mode", "title"),
    [("eq", "EQ"), ("speaker", "Speaker setup"), ("output", "Output")],
)
def test_index_html_renders_the_page_shell_for_its_mode(page_mode, title):
    """All three modes share the design system and the static module; only the
    EQ mode renders the Off/Saved/Draft chrome, and none inlines logic."""
    html = sound_setup._index_html(page_mode=page_mode).decode()

    assert "/assets/app.css" in html
    assert "/assets/sound-profile/sound.css?v=" in html  # linked, not inlined
    assert "<style>" not in html
    assert f'class="app-header__title">{title}' in html
    assert 'id="sound-page-data"' in html
    assert f'"mode": "{page_mode}"' in html
    # The editor is a static ES module (served + revalidated by nginx), the
    # same delivery model as /system/, with no inline logic left in the page.
    module = "speaker" if page_mode == "speaker" else "main"
    assert f'<script type="module" src="/assets/sound-profile/js/{module}.js">' in html
    assert "<script>" not in html

    if page_mode == "eq":
        assert all(marker in html for marker in _EQ_ONLY_CHROME[:3])
        # The id the editor hides sits on the header's tabs WRAPPER: hiding an
        # inner div would leave the wrapper's bottom border behind.
        assert '<div class="app-header__tabs" id="eq-tabs">' in html
    else:
        assert 'id="view-body"' in html
        assert not any(marker in html for marker in _EQ_ONLY_CHROME)
    assert 'id="seat-level-card"' not in html


@pytest.mark.parametrize(
    ("header", "title"),
    [
        ("eq", "EQ"),
        ("speaker", "Speaker setup"),
        ("output", "Output"),
        # nginx sets the header on every page block, so an absent or unknown
        # value only reaches the daemon by a hand-typed request: answer with
        # the one page every profile serves rather than guessing.
        ("", "EQ"),
        ("setup", "EQ"),
    ],
)
def test_the_page_mode_header_picks_the_page(tmp_path: Path, header, title):
    """`X-JTS-Sound-Page` is the whole seam between one daemon and three URLs
    (ADR-0253 §3): nginx sets it per location block and strips the prefix."""
    with sound_server(tmp_path) as base:
        request = urllib.request.Request(base + "/")
        if header:
            request.add_header("X-JTS-Sound-Page", header)
        html = urllib.request.urlopen(request, timeout=5).read().decode()

    assert f"<title>{title}</title>" in html
    assert f'class="app-header__title">{title}' in html


def _island_payload(html: str, element_id="sound-page-data") -> dict:
    marker = f'id="{element_id}"'
    start = html.index(">", html.index(marker)) + 1
    return json.loads(html[start : html.index("</script>", start)])


@pytest.mark.parametrize("follower", [False, True])
@pytest.mark.parametrize("page_mode", ["eq", "speaker", "output"])
def test_sound_page_island_carries_page_identity(monkeypatch, follower, page_mode):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: follower)
    assert _island_payload(sound_setup._index_html(page_mode=page_mode).decode()) == {
        "mode": page_mode, "follower": follower,
    }


@pytest.mark.parametrize("manual,code", [
    ({"crossover_candidates": [{"between_roles": ["woofer", "tweeter"],
      "frequency_hz": 2500, "filter_type": "Butterworth", "slope_db_per_octave": 24}]}, "unsupported_filter_type"),
    ({"drivers": {}}, "field_not_list"),
    ({"drivers": [{"role": "woofer", "pad": {"kind": "direct_db", "attenuation_db": 1}}]}, "pad_attenuation_positive"),
    ({"drivers": [{"role": "woofer", "installation": []}]}, "field_not_object"),
    ({"drivers": [{"role": "woofer", "required_protection_filters": [{"kind": "highpass"}]}]}, "protection_filter_numbers_missing"),
    ({"drivers": [{"role": "woofer", "target_id": "unknown"}]}, "manual_target_unknown"),
])
def test_design_draft_save_carries_field_refusals(monkeypatch, tmp_path, manual, code):
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "active_speaker_design_draft.json"),
    )
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    with output_topology_mutation(topology_path) as mutation:
        mutation.save(mono_output_topology(card_id=None))
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    body = json.dumps({"manual_settings": manual}).encode()
    response, _ = _drive_raw_sound_post(
        tmp_path, path="/active-speaker/design-draft", content_length=len(body), body=body,
    )
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["code"] == code


def test_eq_page_delegates_content_dsp_when_bonded_follower(monkeypatch):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    leader_paths = []
    monkeypatch.setattr(
        sound_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": leader_paths.append(path) or "http://jts3.local/sound/eq/",
    )

    html = sound_setup._index_html("csrf-token", page_mode="eq").decode()

    # The delegation card stays: content EQ / room correction / volume shaping
    # are the leader's job while paired.
    assert "Sound is controlled by the pair leader" in html
    assert leader_paths == ["/sound/eq/"]
    assert "http://jts3.local/sound/eq/" in html
    assert 'href="/sound/speaker/">Open local speaker setup</a>' in html
    # EQ is entirely leader-owned on a follower; no local commissioning module.
    assert "/assets/sound-profile/js/main.js" not in html
    assert 'id="sound-page-data"' in html
    assert '"follower"' in html
    assert 'id="view-body"' not in html
    # The content-EQ editor chrome (Off/Saved/Draft tabs, the segmented tablist,
    # and the now-playing EQ plot) stays delegated to the leader — none of it is
    # rendered on the follower page.
    assert 'id="tab-off"' not in html
    assert 'id="tab-saved"' not in html
    assert 'id="tab-draft"' not in html
    assert 'id="plot"' not in html
    assert 'class="now-playing"' not in html
    assert 'role="tablist"' not in html
    assert 'meta name="jts-csrf" content="csrf-token"' in html


def test_speaker_page_keeps_local_commissioning_when_bonded_follower(monkeypatch):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    leader_paths = []
    monkeypatch.setattr(
        sound_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": leader_paths.append(path)
        or "http://jts3.local/sound/speaker/",
    )

    html = sound_setup._index_html("csrf-token", page_mode="speaker").decode()

    assert leader_paths == ["/sound/speaker/"]
    assert "http://jts3.local/sound/speaker/" in html
    assert 'id="view-body"' in html
    assert "/assets/sound-profile/js/speaker.js" in html
    assert '"mode": "speaker"' in html
    assert '"follower": true' in html
    assert 'id="tab-off"' not in html
    assert 'id="plot"' not in html
    # The local page owns the driver domain, so it offers no way back to it.
    assert "Open local speaker setup" not in html


def test_output_page_delegates_volume_shaping_when_bonded_follower(monkeypatch):
    """Volume shaping is the leader's PROGRAM domain, so the follower's Output
    page is delegation-only — with a path to the local page it does own."""
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    leader_paths = []
    monkeypatch.setattr(
        sound_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": leader_paths.append(path) or "http://jts3.local/sound/output/",
    )

    html = sound_setup._index_html("csrf-token", page_mode="output").decode()

    assert leader_paths == ["/sound/output/"]
    assert "http://jts3.local/sound/output/" in html
    assert 'href="/sound/speaker/">Open local speaker setup</a>' in html
    assert "/assets/sound-profile/js/main.js" not in html
    assert 'id="view-body"' not in html
    # The crossover row hangs under Speaker setup, not this page.
    assert _crossover_child_row()[1] + "</a>" not in html


def test_bonded_follower_rejects_content_dsp_mutations(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(
            base,
            "/settings",
            {},
            expect_status=409,
        )
        payload = json.loads(resp.read().decode("utf-8"))
        assert "controlled on the pair leader" in payload["error"]


def test_follower_block_set_is_content_dsp_only():
    """Invariant 6 (static): the follower POST gate covers only content-DSP
    endpoints. The active-speaker commissioning/crossover endpoints are local
    driver work and must never be in the block set."""
    blocked = sound_setup._FOLLOWER_BLOCKED_CONTENT_DSP_POSTS
    assert blocked == frozenset({
        "/apply",
        "/audition",
        "/live-draft",
        "/cardioid-compare",
        "/settings",
        "/volume-floor/audition",
        "/volume-floor/stop",
        "/profiles/save",
        "/profiles/rename",
        "/profiles/delete",
    })
    assert not any(path.startswith("/active-speaker/") for path in blocked)


def test_bonded_follower_allows_active_speaker_endpoints(monkeypatch, tmp_path: Path):
    """Invariant 6 (live): on a follower an active-speaker read returns 200 and a
    commissioning/crossover POST reaches its handler (never 404/409), while a
    content-DSP POST still 409s. Local driver work stays with the DAC owner."""
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    with sound_server(tmp_path) as base:
        session = make_csrf_session(base, "/")
        # A content-DSP mutation is delegated to the leader.
        assert _follower_post_status(base, "/settings", session) == 409
        # An active-speaker read is served (200) — the GET path has no follower gate.
        assert (
            _follower_get_status(base, "/active-speaker/design-draft", session) == 200
        )
        active_status = _follower_post_status(
            base, "/active-speaker/rear-calibration/validate", session,
        )
        assert active_status not in (404, 409), active_status


def test_index_html_embeds_csrf_meta_for_json_posts():
    html = sound_setup._index_html("csrf-token").decode()
    # The token rides in the meta tag; the static module reads it and sends
    # X-CSRF-Token on every mutating POST.
    assert 'meta name="jts-csrf" content="csrf-token"' in html


def test_i2s_hat_payload_offers_only_the_undetectable_hats(monkeypatch, tmp_path):
    """Detected HATs are reported, never offered as a choice (ADR-0234)."""

    intent = tmp_path / "i2s_hat.env"
    hat_dir = tmp_path / "hat"
    marker = tmp_path / "i2s-reboot"
    marker.touch()
    hardware = {"usb_data_role": {"board_topology": "shared_otg_port"}}
    monkeypatch.setattr(sound_active_speaker, "I2S_HAT_REBOOT_REQUIRED_PATH", str(marker))
    monkeypatch.setattr(sound_active_speaker, "_output_hardware_dict", lambda: hardware)

    payload = sound_active_speaker._i2s_hat_payload(intent_path=intent, hat_dir=hat_dir)

    detectable = {p.id for p in dac_all_profiles() if p.hat_products}
    offered = {entry["id"] for entry in payload["profiles"]}
    assert "innomaker_hifi_amp_pro" in offered
    assert not offered & detectable
    assert payload["desired_profile_id"] is None
    assert payload["detected_profile_id"] is None
    assert payload["detected_label"] == ""
    assert payload["restart_required"] is True
    assert payload["visibility"] == "visible"
    assert payload["available"] is True
    assert payload["shared_usb_data_port"] is True

    write_hat_eeprom(hat_dir, product="StudioDAC8x")
    detected = sound_active_speaker._i2s_hat_payload(intent_path=intent, hat_dir=hat_dir)

    assert detected["detected_profile_id"] == "hifiberry_dac8x_studio"
    assert detected["detected_label"] == next(
        p.label for p in dac_all_profiles() if p.id == "hifiberry_dac8x_studio"
    )
    assert "hifiberry_dac8x_studio" not in {e["id"] for e in detected["profiles"]}

    # A board the reconciler will not manage reports no detection either.
    hardware.clear()
    hardware["usb_data_role"] = {"board_topology": "unsupported"}
    unsupported = sound_active_speaker._i2s_hat_payload(intent_path=intent, hat_dir=hat_dir)
    assert unsupported["available"] is False
    assert unsupported["detected_profile_id"] is None
    assert unsupported["detected_label"] == ""


def test_i2s_hat_payload_surfaces_a_boot_config_collision(monkeypatch, tmp_path):
    intent = tmp_path / "i2s_hat.env"
    boot = tmp_path / "config.txt"
    boot.write_text("[all]\ndtoverlay=merus-amp\n", encoding="utf-8")
    sound_active_speaker.write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)
    monkeypatch.setattr(
        sound_active_speaker, "_output_hardware_dict",
        lambda: {"usb_data_role": {"board_topology": "shared_otg_port"}},
    )

    payload = sound_active_speaker._i2s_hat_payload(
        intent_path=intent, boot_config_path=boot, hat_dir=tmp_path / "hat"
    )

    assert payload["desired_profile_id"] == "innomaker_hifi_amp_pro"
    assert len(payload["warnings"]) == 1
    assert isinstance(payload["warnings"][0], str)


def test_i2s_hat_save_reuses_start_only_reconcile_broker(monkeypatch):
    from jasper.control import restart_broker

    calls = []
    monkeypatch.setattr(
        sound_active_speaker,
        "_i2s_hat_payload",
        lambda: {"available": True, "reason": "", "restart_required": True,
                 "warnings": ["collision"]},
    )
    monkeypatch.setattr(
        sound_active_speaker,
        "write_i2s_hat_intent",
        lambda profile_id: calls.append(("write", profile_id)),
    )

    def manage(unit, **kwargs):
        calls.append((unit, kwargs))
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", manage)

    payload, result = sound_setup._save_i2s_hat_payload("innomaker_hifi_amp_pro")

    assert result == {"ok": True}
    assert payload["warnings"] == ["collision"]
    assert calls[0] == ("write", "innomaker_hifi_amp_pro")
    unit, options = calls[1]
    assert unit == "jasper-audio-hardware-reconcile.service"
    assert options["verb"] == "start"
    assert options["no_block"] is False
    assert options["timeout"] == 55.0

    def fail_apply(*_args, **_kwargs):
        raise OSError("broker unavailable")

    monkeypatch.setattr(restart_broker, "manage_units", fail_apply)
    refreshed, failed = sound_setup._save_i2s_hat_payload(None)
    assert refreshed["restart_required"] is True
    assert refreshed["warnings"] == ["collision"]
    assert failed == {"ok": False, "error": "broker unavailable"}


def test_active_speaker_stop_and_level_payloads_are_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration-level.json"),
    )
    environment = {
        "status": "pass",
        "load_gate": "ready",
        "ok_to_load_active_config": True,
        "camilla_config": {
            "classification": "active_startup_candidate",
            "path": "/tmp/active.yml",
        },
        "safe_playback": {
            "status": "not_implemented",
            "playback_allowed": False,
        },
        "issues": [],
    }

    from jasper.active_speaker.safe_playback import arm_safe_playback_session

    armed = arm_safe_playback_session(environment)
    guarded = update_calibration_level_state(
        action="set", requested_level_dbfs=-55, run_id=armed["session_id"],
    )
    status = load_safe_playback_state()
    stopped = sound_active_speaker._active_speaker_stop_payload()
    stopped_level = load_calibration_level_state()

    assert armed["status"] == "armed"
    assert armed["playback_allowed"] is False
    assert guarded["test_signal"]["requested_level_dbfs"] == -70.0
    assert guarded["issues"][0]["code"] == "upward_step_limited"
    assert status["status"] == "armed"
    assert stopped["status"] == "stopped"
    assert stopped["playback"]["status"] == "stopped"
    assert stopped["session_id"] == armed["session_id"]
    assert stopped["calibration_level"]["test_signal"]["requested_level_dbfs"] == -80.0
    assert stopped_level["test_signal"]["requested_level_dbfs"] == -80.0


def test_active_speaker_stop_payload_survives_level_reset_failure(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.active_speaker import calibration_level as level_mod

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
        str(tmp_path / "safe-playback.json"),
    )
    environment = {
        "status": "pass",
        "load_gate": "ready",
        "ok_to_load_active_config": True,
        "camilla_config": {
            "classification": "active_startup_candidate",
            "path": "/tmp/active.yml",
        },
        "safe_playback": {
            "status": "not_implemented",
            "playback_allowed": False,
        },
        "issues": [],
    }

    def fail_reset(*args, **kwargs):
        raise OSError("state path is unavailable")

    from jasper.active_speaker.safe_playback import arm_safe_playback_session

    arm_safe_playback_session(environment)
    monkeypatch.setattr(level_mod, "update_calibration_level_state", fail_reset)

    stopped = sound_active_speaker._active_speaker_stop_payload()

    assert stopped["status"] == "stopped"
    assert stopped["playback"]["status"] == "stopped"
    assert stopped["calibration_level"]["status"] == "reset_failed"


def _active_speaker_mono_topology_payload(
    *,
    card_id: str | None = "DAC8",
    identity_verified: bool = False,
) -> dict:
    woofer: dict = {"role": "woofer", "physical_output_index": 0}
    tweeter: dict = {
        "role": "tweeter",
        "physical_output_index": 1,
        "startup_muted": True,
        "protection_required": True,
    }
    if identity_verified:
        woofer["identity_verified"] = True
        tweeter["identity_verified"] = True
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": card_id,
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [woofer, tweeter],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    }


def _passive_left_topology_payload() -> dict:
    """One unverified passive full-range lane on DAC output 1."""

    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            }
        ],
        "routing": {"main_left_group_id": "left"},
    }


def _bench_active_topology_payload() -> dict:
    """A mono active 2-way with neither lane confirmed and no tweeter guard."""

    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_active",
        "name": "Bench active",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {"role": "tweeter", "physical_output_index": 1},
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    }


_ACTIVE_SPEAKER_STATE_FILENAMES = {
    "JASPER_OUTPUT_TOPOLOGY_PATH": "output_topology.json",
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design_draft.json",
    "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR": "tone-artifacts",
    "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE": "safe-playback.json",
    "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH": "active_staged.yml",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "active_staged.json",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path_safety.json",
    "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE": "startup_load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE": "commission_load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE": "commission_ramp.json",
    "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE": "baseline_profile.json",
    "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH": "active_speaker_baseline.yml",
}


def _set_active_speaker_state_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *extra_env_names: str,
) -> dict[str, Path]:
    """Point one test at isolated topology/draft/preview state plus extras."""

    env_names = (
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        *extra_env_names,
    )
    paths = {
        name: tmp_path / _ACTIVE_SPEAKER_STATE_FILENAMES[name]
        for name in env_names
    }
    for name, path in paths.items():
        monkeypatch.setenv(name, str(path))
    return paths


def _passive_stereo_with_sub_topology_payload(
    *,
    crossover_fc_hz: float | None = None,
) -> dict:
    """Passive stereo mains + one local subwoofer on the next contiguous output.

    The subwoofer card writes ``crossover_fc_hz`` onto the sub channel; pass it
    through here to exercise the topology save round-trip.
    """

    sub_channel: dict = {
        "role": "subwoofer",
        "physical_output_index": 2,
        "identity_verified": True,
    }
    if crossover_fc_hz is not None:
        sub_channel["crossover_fc_hz"] = crossover_fc_hz
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_stereo_sub",
        "name": "Bench stereo + sub",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [
                    {
                        "role": "full_range",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    }
                ],
            },
            {
                "id": "right",
                "label": "Right",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [
                    {
                        "role": "full_range",
                        "physical_output_index": 1,
                        "identity_verified": True,
                    }
                ],
            },
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [sub_channel],
            },
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    }


def _sub_channel_from_saved(saved: dict) -> dict:
    for group in saved["output_topology"]["speaker_groups"]:
        if group.get("kind") == "subwoofer" or group.get("mode") == "subwoofer":
            return group["channels"][0]
    raise AssertionError("no subwoofer group in saved topology")


INNOMAKER_DEVICE_ID = "innomaker_hifi_amp_pro"
INNOMAKER_DEVICE_LABEL = "InnoMaker HiFi AMP Pro"


def _innomaker_topology_payload(*, active: bool, subwoofer: bool = False) -> dict:
    """A save posted against the two-output InnoMaker HiFi AMP Pro.

    ``active=True`` is the mono active 2-way layout the wizard REFUSED on this
    board until it gained the width-2 active outputd lane. The refusal was
    correct while the board declared no lane — the save left a box structurally
    mute, CamillaDSP running the roleful graph into an aloop lane nothing
    drains while outputd captured the passive one.

    ``subwoofer=True`` is the second roleful shape a household reaches through
    the wizard: passive mono mains plus a local sub. It is roleful through the
    subwoofer branch rather than an active crossover.

    Both roleful shapes are ACCEPTED here now. The refusal itself is still
    pinned, against ``_no_lane_topology_payload`` below.
    """

    if subwoofer:
        groups = [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "full_range_passive",
                "channels": [
                    {
                        "role": "full_range",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    }
                ],
            },
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [
                    {
                        "role": "subwoofer",
                        "physical_output_index": 1,
                        "identity_verified": True,
                    }
                ],
            },
        ]
        routing = {"mono_group_id": "mono", "subwoofer_group_ids": ["sub"]}
    elif active:
        groups = [
            {
                "id": "main",
                "label": "Main active speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                    },
                ],
            }
        ]
        routing = {"mono_group_id": "main"}
    else:
        groups = [
            {
                "id": "left",
                "label": "Left",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [
                    {
                        "role": "full_range",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    }
                ],
            },
            {
                "id": "right",
                "label": "Right",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [
                    {
                        "role": "full_range",
                        "physical_output_index": 1,
                        "identity_verified": True,
                    }
                ],
            },
        ]
        routing = {"main_left_group_id": "left", "main_right_group_id": "right"}
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "default",
        "name": "Bench InnoMaker",
        "status": "draft",
        "hardware": {
            "device_id": INNOMAKER_DEVICE_ID,
            "device_label": INNOMAKER_DEVICE_LABEL,
            "physical_output_count": 2,
            "card_id": "sndrpimerusamp",
        },
        "speaker_groups": groups,
        "routing": routing,
    }


def _no_lane_topology_payload(*, active: bool, subwoofer: bool = False) -> dict:
    """The same roleful shapes, posted against a DAC that declares NO lane.

    The save guard's subject. Every profile in the shipped registry now declares
    an active lane, so the guard is pinned against the synthetic stand-in for the
    NEXT passive-only DAC — the population it actually protects — rather than
    against whichever real profile happens not to have been flipped yet.
    """

    payload = _innomaker_topology_payload(active=active, subwoofer=subwoofer)
    payload["name"] = PASSIVE_ONLY_DAC_LABEL
    payload["hardware"] = {
        "device_id": PASSIVE_ONLY_DAC_ID,
        "device_label": PASSIVE_ONLY_DAC_LABEL,
        "physical_output_count": 2,
        "card_id": "benchpassive",
    }
    return payload


@pytest.mark.parametrize("output_index", [0, None])
def test_duplicate_dac_outputs_are_refused_and_unassigned_ones_save(monkeypatch, tmp_path, output_index):
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))
    payload = _passive_left_topology_payload()
    payload["speaker_groups"].append({
        "id": "right",
        "label": "Right speaker",
        "kind": "right",
        "mode": "full_range_passive",
        "channels": [{"role": "full_range", "physical_output_index": output_index}],
    })
    payload["routing"]["main_right_group_id"] = "right"

    if output_index is None:
        # A half-assigned layout is a supported stored state (#2145).
        saved = sound_setup._save_output_topology_payload(payload)
        assert saved["output_topology"]["speaker_groups"][1]["channels"][0]["physical_output_index"] is None
        assert topo_path.exists()
        return
    with pytest.raises(OutputTopologyError):
        sound_setup._save_output_topology_payload(payload)

    assert not topo_path.exists()


@pytest.mark.parametrize(
    ("shape", "named_in_refusal"),
    [
        # Passive is not a free remedy: it sends full-range into every assigned
        # output, which on an actively-wired cabinet reaches a bare tweeter. The
        # household is being steered there, so the consequence travels with it.
        (
            {"active": True},
            (
                "full-range audio to every output",
                "built-in passive crossover",
                "attach an active-capable DAC",
            ),
        ),
        # The subwoofer branch is roleful too, and the wizard offers it as a
        # one-tap add-on, so the copy has to name it.
        ({"active": False, "subwoofer": True}, ("subwoofer layouts",)),
    ],
)
def test_a_roleful_layout_on_a_dac_without_an_active_lane_is_refused(
    monkeypatch,
    tmp_path: Path,
    shape,
    named_in_refusal,
):
    """Save-time capability guard: the wizard must not accept a layout this
    box can never drive. Before this guard the save landed with blockers=0 and
    the speaker went silent with every daemon reporting healthy."""
    register_passive_only_dac(monkeypatch)
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))

    with pytest.raises(ValueError) as excinfo:
        sound_setup._save_output_topology_payload(_no_lane_topology_payload(**shape))

    message = str(excinfo.value)
    assert PASSIVE_ONLY_DAC_LABEL in message
    assert all(fragment in message for fragment in named_in_refusal)
    # Refused means refused: nothing was written.
    assert not topo_path.exists()


@pytest.mark.parametrize("subwoofer_supported, assigned", [(True, True), (True, False), (False, True)])
def test_layout_save_refuses_active_route_over_capacity(monkeypatch, tmp_path, caplog, subwoofer_supported, assigned):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    payload = _active_speaker_mono_topology_payload()
    payload["hardware"]["device_id"] = "hifiberry_dac8x"
    payload["hardware"]["physical_output_count"] = 8
    payload["speaker_groups"] = [{
        "id": side, "label": side, "kind": side, "mode": "active_3_way",
        "channels": [{"role": role, "physical_output_index": index + offset}
                     for index, role in enumerate(("woofer", "mid", "tweeter"))],
    } for side, offset in (("left", 0), ("right", 3))]
    payload["routing"] = {"main_left_group_id": "left", "main_right_group_id": "right"}
    if not assigned:
        # A half-assigned layout is a supported stored state (#2145); it still
        # needs a lane per channel.
        for channel in payload["speaker_groups"][1]["channels"]:
            channel["physical_output_index"] = None
    if not subwoofer_supported:
        sub_layout = _passive_stereo_with_sub_topology_payload()
        payload.update({key: sub_layout[key] for key in ("speaker_groups", "routing")})
    resolve = playback_route.resolve_output_layout
    monkeypatch.setattr(playback_route, "resolve_output_layout", lambda topology, **kwargs:
        replace(resolve(topology, **kwargs), transport_channel_count=4, subwoofer_supported=subwoofer_supported))
    with pytest.raises(sound_active_speaker.OutputTopologyCapabilityBlocked) as caught:
        sound_setup._save_output_topology_payload(payload)
    _, refusal = _event_record(caplog, "sound.output_topology_save")
    assert caught.value.code == refusal["reason"]
    assert refusal["result"] == "blocked"
    assert refusal["reason"] == ("active_playback_route_too_narrow" if subwoofer_supported
                                 else "active_playback_subwoofer_not_supported")
    assert int(refusal["required_active_output_count"]) == (6 if subwoofer_supported else 3)
    assert int(refusal["transport_channel_count"]) == 4
    assert not path.exists()


def test_passive_layout_on_a_no_lane_dac_still_saves(monkeypatch, tmp_path: Path):
    """The guard keys strictly on rolefulness, so the ordinary passive stereo
    layout a lane-less board DOES support is untouched."""
    register_passive_only_dac(monkeypatch)
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))

    saved = sound_setup._save_output_topology_payload(
        _no_lane_topology_payload(active=False)
    )

    assert saved["output_topology"]["hardware"]["device_id"] == PASSIVE_ONLY_DAC_ID
    assert topo_path.exists()


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param({"active": True}, id="active_2_way"),
        pytest.param({"active": False, "subwoofer": True}, id="local_sub"),
    ],
)
def test_a_roleful_layout_on_the_innomaker_is_accepted_with_a_drivable_route(
    monkeypatch, tmp_path: Path, shape,
):
    """THE FLIP, at the surface the owner hit: /sound/speaker/ refused these
    layouts on the InnoMaker, and now accepts them.

    The board declares the width-2 active outputd lane, so the one predicate
    behind the refusal (``active_lane_capability_gap``) no longer fires for
    either roleful shape and the layout persists. Accepting a layout is NOT
    arming it — the reconciler still gates active mode on a legal active graph
    already being live, which only commissioning produces; that fail-closed
    property is pinned in tests/test_audio_hardware_reconcile.py. Acceptance
    still has to come with a drivable route, which is the second half here.
    """
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))

    saved = sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(**shape)
    )

    assert saved["output_topology"]["hardware"]["device_id"] == INNOMAKER_DEVICE_ID
    assert topo_path.exists()
    roleful = saved["output_topology"]["speaker_groups"][-1]
    assert roleful["mode"] == ("active_2_way" if shape["active"] else "subwoofer")


def test_topology_save_kicks_hardware_and_grouping_reconcile(
    monkeypatch,
    tmp_path: Path,
):
    """A save converges hardware and revokes any stale grouping DAC bypass."""
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    calls: list[dict] = []
    sentinel = {"ok": True, "action": "start"}
    grouping_env = tmp_path / "grouping-outputd.env"
    grouping_env.write_text("JASPER_OUTPUTD_DAC_CONTENT_LANE=1\n")
    grouping_complete = False

    def fake_manage_units(*units, **kwargs):
        nonlocal grouping_complete
        calls.append({"units": units, **kwargs})
        if units == ("jasper-outputd.service",):
            return {"ok": True}
        if units == ("jasper-grouping-reconcile.service",):
            assert not any(
                call["units"] == ("jasper-audio-hardware-reconcile.service",)
                for call in calls
            )
            grouping_env.write_text("JASPER_OUTPUTD_DAC_CONTENT_LANE=\n")
            grouping_complete = True
        if units == ("jasper-audio-hardware-reconcile.service",):
            assert grouping_complete is True
        return sentinel

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units", fake_manage_units
    )
    _stub_audio_stops(monkeypatch)

    saved = sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )

    assert calls[0]["units"] == ("jasper-outputd.service",)
    assert calls[0]["verb"] == "stop"
    assert calls[1]["units"] == ("jasper-grouping-reconcile.service",)
    assert calls[1]["verb"] == "start"
    assert calls[1]["reason"] == "output_topology_save"
    assert calls[2]["units"] == ("jasper-audio-hardware-reconcile.service",)
    assert calls[2]["verb"] == "start"
    # A topology replacement first parks audio, then waits for the root
    # reconciler to make outputd agree with the final saved topology.
    assert calls[1]["no_block"] is False
    assert grouping_env.read_text() == "JASPER_OUTPUTD_DAC_CONTENT_LANE=\n"
    assert saved["reconcile"] is sentinel


# The two reconcile outcomes every topology-mutating endpoint has to tell
# apart (#3094): a reconcile past its own wait budget is still working, and
# saying "needs attention" for it sends a household to Status for nothing.
RECONCILE_STILL_CONVERGING = {"ok": False, "converging": True}
RECONCILE_FAILED = {"ok": False, "error": "private backend detail"}


def _stub_reconcile(monkeypatch, result: dict) -> None:
    monkeypatch.setattr(
        "jasper.output_topology_runtime.trigger_reconcile",
        lambda **_kwargs: result,
    )


@pytest.mark.parametrize(
    ("reconcile", "status"),
    [
        (RECONCILE_FAILED, "needs_attention"),
        (RECONCILE_STILL_CONVERGING, "converging"),
    ],
)
def test_topology_save_reports_the_reconcile_verdict_without_leaking_it(
    monkeypatch, tmp_path: Path, reconcile, status,
):
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "topology.json"))
    _stub_audio_stops(monkeypatch)
    _stub_reconcile(monkeypatch, reconcile)

    saved = sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )

    assert saved["save"]["status"] == status
    assert "private backend detail" not in saved["save"]["message"]


def test_topology_save_parks_before_replacing_saved_layout(
    monkeypatch, tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    original = new_topology_draft(name="Old layout")
    save_output_topology(original, path=path)
    seen: list[OutputTopology] = []

    def park_and_commit(topology, commit, **_kwargs):
        assert load_output_topology() == original
        seen.append(topology)
        return _RuntimeMutation(commit())

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )
    monkeypatch.setattr(
        "jasper.output_topology_runtime.trigger_reconcile",
        lambda **_kwargs: {"ok": True},
    )
    _stub_audio_stops(monkeypatch)

    saved = sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )

    assert seen == [original]
    assert saved["output_topology"]["name"] != original.name


@pytest.mark.parametrize("applied", [False, True])
def test_topology_resave_converges_without_parking(monkeypatch, tmp_path, caplog, applied):
    raw = _active_speaker_mono_topology_payload(
    )
    save_output_topology(OutputTopology.from_mapping(raw))
    prior_path = str(tmp_path / "baseline.yml")
    Path(prior_path).write_text(_active_baseline_yaml("mono", 2))
    controller = Mock(spec=FakeCamilla)
    controller._graph_mutation_lock_path = tmp_path / "graph.lock"
    controller.get_config_file_path.return_value = prior_path
    controller.set_config_file_path.return_value = applied
    manage_units = Mock()
    monkeypatch.setattr("jasper.control.restart_broker.manage_units", manage_units)
    monkeypatch.setattr("jasper.camilla.primary_controller", lambda: controller)
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit_topology,
    )
    _stub_audio_stops(monkeypatch)
    _stub_reconcile(monkeypatch, {"ok": True})
    caplog.set_level(logging.INFO, logger=sound_active_speaker.logger.name)

    result = sound_setup._save_output_topology_payload({"output_topology": raw})

    assert manage_units.mock_calls == []
    assert controller.mock_calls == [
        call.get_config_file_path(best_effort=True),
        call.set_config_file_path(prior_path, best_effort=True),
    ]
    assert PARK_SKIPPED.to_dict() == {
        "ok": False, "decision": None, "live_applied": False, "error": None,
    }
    assert result["save"]["status"] == ("saved" if applied else "needs_attention")
    assert result["runtime_convergence"]["ok"] is applied
    assert result["runtime_convergence"]["live_applied"] is applied
    assert result["runtime_convergence"]["decision"]["status"] == "preserve_current"
    assert load_output_topology() == OutputTopology.from_mapping(raw)
    _, fields = _event_record(caplog, "sound.output_topology_save")
    assert fields["parked"] == "false"
    assert fields["live_applied"] == str(applied).lower()


def test_topology_save_does_not_restore_old_graph_for_a_post_write_read_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    save_output_topology(new_topology_draft(name="Old layout"), path)
    real_snapshot = topology_mod.load_output_topology_snapshot
    snapshot_reads = 0
    events: list[str] = []

    def one_snapshot_only(snapshot_path=None):
        nonlocal snapshot_reads
        snapshot_reads += 1
        if snapshot_reads > 1:
            raise OSError("post-publication read failed")
        return real_snapshot(snapshot_path)

    def park_and_commit(_topology, commit, **_kwargs):
        events.append("park")
        committed = commit()
        events.append("converge-new-graph")
        return _RuntimeMutation(committed)

    monkeypatch.setattr(
        topology_mod,
        "load_output_topology_snapshot",
        one_snapshot_only,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )
    monkeypatch.setattr(
        "jasper.output_topology_runtime.trigger_reconcile",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        sound_active_speaker,
        "_output_topology_payload",
        lambda: {"output_topology": {"status": "valid"}},
    )
    _stub_audio_stops(monkeypatch)

    sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )

    assert snapshot_reads == 1
    assert events == ["park", "converge-new-graph"]
    monkeypatch.setattr(topology_mod, "load_output_topology_snapshot", real_snapshot)
    assert load_output_topology(path).name != "Old layout"


def test_topology_save_refuses_invalid_input_before_stopping_or_parking(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "topology.json"))
    monkeypatch.setattr(
        sound_active_speaker,
        "_active_speaker_stop_payload",
        lambda: pytest.fail("invalid input must not stop audio"),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda *_args, **_kwargs: pytest.fail("invalid input must not park audio"),
    )

    with pytest.raises(OutputTopologyError):
        sound_setup._save_output_topology_payload({"name": "not a topology"})


def test_topology_save_stops_audio_sessions_before_parking(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "topology.json"))
    events = _stub_audio_stops(monkeypatch)

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda _topology, commit, **_kwargs: (
            events.append("park") or _RuntimeMutation(commit())
            if events == ["safe"]
            else pytest.fail(f"expected all active audio sessions stopped first: {events}")
        ),
    )

    sound_setup._save_output_topology_payload(_innomaker_topology_payload(active=False))


def test_refused_layout_reaches_the_page_as_a_rendered_error(
    monkeypatch,
    tmp_path: Path,
):
    register_passive_only_dac(monkeypatch)
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))
    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(
            base,
            "/output-topology",
            {
                "output_topology": _no_lane_topology_payload(active=True),
            },
            expect_status=400,
        )
        payload = json.loads(resp.read().decode("utf-8"))

        assert PASSIVE_ONLY_DAC_LABEL in payload["error"]
        assert "output_topology" not in payload
        assert not topo_path.exists()


@pytest.mark.parametrize(
    ("posted_fc_hz", "blocker"),
    [
        (110, None),
        (None, None),
        # An out-of-range corner is a fail-loud topology blocker (never a silent
        # clamp), so the saved topology surfaces the issue rather than emitting
        # a non-band-limiting crossover.
        (999, "subwoofer_crossover_out_of_range"),
    ],
)
def test_subwoofer_crossover_fc_round_trips_through_topology_save(
    monkeypatch,
    tmp_path: Path,
    posted_fc_hz,
    blocker,
):
    """A bass-management corner posted on the sub channel persists verbatim and
    echoes back through ``_save_output_topology_payload`` — the contract the
    ``/sound/`` subwoofer-card Fc control relies on. Left unset, no field is
    written at all, so the active builder falls back to
    ``DEFAULT_SUB_CROSSOVER_HZ``.
    """

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    saved = sound_setup._save_output_topology_payload(
        _passive_stereo_with_sub_topology_payload(crossover_fc_hz=posted_fc_hz)
    )
    channel = _sub_channel_from_saved(saved)
    codes = {
        issue["code"]
        for issue in saved["output_topology"]["evaluation"]["blockers"]
    }

    assert channel.get("crossover_fc_hz") == posted_fc_hz
    assert ("crossover_fc_hz" in channel) is (posted_fc_hz is not None)
    assert (blocker in codes) if blocker else "subwoofer_crossover_out_of_range" not in codes

    # Re-load from disk to prove the value survives serialization, not just the
    # in-memory echo.
    sub_group = next(
        group
        for group in load_output_topology().speaker_groups
        if group.mode == "subwoofer"
    )
    assert sub_group.channels[0].crossover_fc_hz == posted_fc_hz


def _active_speaker_driver_research_payload(*, frequency_hz: float = 2500) -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_crossover_driver_research",
        "drivers": [
            {
                "role": "woofer",
                "model": "Epique E150HE-44",
                "recommended_lowpass_hz": frequency_hz,
                "sources": ["https://example.test/woofer"],
            },
            {
                "role": "tweeter",
                "model": "F110M-8",
                "recommended_highpass_hz": frequency_hz,
                "do_not_test_below_hz": 1200,
                "sources": ["https://example.test/tweeter"],
            },
        ],
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": frequency_hz,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            }
        ],
    }


def _save_active_speaker_design_and_preview(*, frequency_hz: float = 2500) -> dict:
    sound_setup._active_speaker_design_draft_save_payload({
        "operator_inputs": {
            "woofer": "Dayton Epique E150HE-44",
            "tweeter": "Eminence F110M-8",
        },
        "driver_research": _active_speaker_driver_research_payload(
            frequency_hz=frequency_hz,
        ),
    })
    return sound_setup._active_speaker_crossover_preview_payload()


def test_driver_research_prompt_payload_uses_unsaved_models_and_notes(monkeypatch) -> None:
    from tests.active_speaker_fixtures import mono_output_topology

    topology = mono_output_topology(card_id=None)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    payload = sound_setup._active_speaker_driver_research_request_payload({
        "operator_inputs": {"woofer": "Example W6", "tweeter": "Example T1", "notes": "sealed cabinet"},
    })
    assert set(payload) == {"prompt"}
    assert all(model in payload["prompt"] for model in ("Example W6", "Example T1"))
    assert "sealed cabinet" in payload["prompt"]
    with pytest.raises(ValueError):
        sound_setup._active_speaker_driver_research_request_payload({"typo": "unknown"})


@pytest.mark.parametrize("model", [None, "", " \t "])
def test_driver_research_prompt_refuses_a_target_without_a_model(monkeypatch, model) -> None:
    from tests.active_speaker_fixtures import mono_output_topology

    topology = mono_output_topology(card_id=None)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    with pytest.raises(ValueError):
        sound_setup._active_speaker_driver_research_request_payload({
            "operator_inputs": {"woofer": "Example W6", "tweeter": model},
        })


def test_draft_and_preview_preserve_the_saved_topology(
    monkeypatch,
    tmp_path: Path,
):
    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)

    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload()
    )
    path = paths["JASPER_OUTPUT_TOPOLOGY_PATH"]
    before = path.read_bytes(), path.stat().st_mtime_ns
    refreshed = _save_active_speaker_design_and_preview()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    assert refreshed["status"] == "ready_for_protected_staging"
    filters = refreshed["groups"][0]["crossovers"][0]["filters"]
    tweeter_filter = next(
        item for item in filters
        if item["role"] == "tweeter"
    )
    assert tweeter_filter["channel"]["protection_required"] is True


def _record_dac8x() -> None:
    """The reconciler's record for a ready DAC8x, at the path conftest isolates."""
    write_output_hardware_state(
        OutputHardwareState(
            profile_id="hifiberry_dac8x",
            profile_label="HiFiBerry DAC8x",
            status="ready",
            physical_output_count=8,
            selected_card_id="sndrpihifiberry",
            selected_pcm="hw:CARD=sndrpihifiberry,DEV=0",
            child_devices=(
                OutputCardFact(card_id="sndrpihifiberry", device_id="hifiberry_dac8x"),
            ),
        ),
        os.environ["JASPER_OUTPUT_HARDWARE_STATE_PATH"],
    )


def test_output_topology_payload_does_not_take_mutation_lock(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setattr(
        sound_active_speaker,
        "output_topology_mutation",
        lambda: pytest.fail("read-only payload must not take the mutation lock"),
    )

    assert sound_setup._output_topology_payload()["output_topology"]["status"] == "draft"


def test_output_topology_payload_serializes_with_populated_hardware_state(
    monkeypatch,
    tmp_path: Path,
):
    """A populated output-hardware state file must not 502 the route.

    ``load_state`` returns a frozen ``OutputHardwareState`` whenever a state
    file exists (every real Pi) and ``_send_json`` emits with plain
    ``json.dumps``, which cannot encode a dataclass.
    """
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    card = OutputCardFact(
        card_id="A",
        pcm="hw:A,0",
        device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        label="Apple USB-C dongle",
        has_playback=True,
    )
    write_output_hardware_state(classify_output_cards([card]))

    envelope = sound_setup._output_topology_payload()

    # The exact serialization _send_json performs — this raised the 502.
    json.dumps(envelope)
    hardware = envelope["output_hardware"]
    assert isinstance(hardware, dict)
    assert hardware["status"] == "ready"
    assert envelope["hardware_adoption"]["allowed"] is True
    # #2812 S5: the JS mismatch card (and #2819's re-pin offer nested inside
    # it) is a pure proxy for this key now — it does not recompute the rule
    # itself. Deleting the key here would silently kill both with every test
    # elsewhere still green (the JS harness fixture supplies its own value
    # independent of this payload builder), so the key's presence is pinned at
    # its one source.
    assert "hardware_mismatch" in envelope


@pytest.mark.parametrize("spacing", [{}, {"driver_spacing_mm": None}, {"driver_spacing_mm": 200}])
def test_driver_spacing_draft_save_reaches_geometry_and_handoff(monkeypatch, tmp_path, spacing):
    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)
    topology = mono_output_topology(card_id=None)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    saved = sound_setup._active_speaker_design_draft_save_payload({
        "manual_settings": {"drivers": [{"role": "woofer", "model": "Test woofer"}], **spacing},
    })
    loaded = load_design_draft(topology=topology, path=paths["JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"])
    expected = spacing.get("driver_spacing_mm")
    assert saved["manual_settings"]["driver_spacing_mm"] == expected
    assert loaded["manual_settings"]["driver_spacing_mm"] == expected
    geometry = MeasurementGeometry(driver_spacing_m=declared_driver_spacing_m(loaded) or 0.0, mic_distance_m=1.0)
    assert geometry.parallax_us() == pytest.approx(57.7 if expected else 0.0, abs=0.05)
    view = build_commissioning_view(topology, design_draft=loaded)
    assert view["driver_spacing_mm"] == expected
    assert build_tuning_handoff(commissioning_view=view, design_draft=loaded)["driver_spacing_mm"] == expected


@pytest.mark.parametrize("field", ["confirm_safety_profile", "typo"])
def test_design_draft_save_payload_refuses_unknown_fields(field) -> None:
    with pytest.raises(ValueError):
        sound_setup._active_speaker_design_draft_save_payload({field: True})


def test_design_draft_save_without_expected_revision_succeeds(monkeypatch, tmp_path: Path) -> None:
    from tests.active_speaker_fixtures import mono_output_topology

    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)
    topology = mono_output_topology(card_id=None)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    saved = sound_setup._active_speaker_design_draft_save_payload({"operator_inputs": {"notes": "current"}})
    assert saved["revision"] == 1
    assert json.loads(paths["JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"].read_text())["operator_inputs"] == {"notes": "current"}


def test_preview_preserves_driver_values_and_does_not_rewrite_draft(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jasper.active_speaker.driver_safety import build_driver_research_context
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.test_active_speaker_driver_safety import (
        _manual_settings,
        _operator_inputs,
        _research_result,
    )

    topology = mono_output_topology(card_id=None)
    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)
    draft_path = paths["JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"]

    save_output_topology(topology)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    request = build_driver_research_context(
        topology,
        _operator_inputs(),
    )
    saved = sound_setup._active_speaker_design_draft_save_payload({
        "driver_research": _research_result(request),
        "manual_settings": _manual_settings(),
        "operator_inputs": _operator_inputs(),
    })
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["updated_at"] = "2026-08-14T16:33:48Z"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    before = draft_path.read_bytes()

    preview = sound_setup._active_speaker_crossover_preview_payload()

    after = draft_path.read_bytes()
    loaded = sound_setup._active_speaker_design_draft_payload()
    assert after == before
    assert loaded["revision"] == saved["revision"] == 1
    assert loaded["driver_research"] == saved["driver_research"]
    assert loaded["driver_safety_profile"] == (
        saved["driver_safety_profile"]
    )
    assert preview["source"]["design_draft_updated_at"] == draft["updated_at"]


def _declared_candidate_box(
    monkeypatch,
    tmp_path: Path,
    *,
    slope_db_per_octave: int | None = 24,
    operator_inputs: dict | None = None,
) -> list[int]:
    """Save one declared woofer/tweeter candidate at 5500 Hz, revision 1.

    Returns the recorded ``os.fsync`` fd list, already asserted empty: an
    ordinary wizard design-draft save must not fsync (#2292), so that half is
    checked at every call site rather than in one test.
    """
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.test_active_speaker_driver_safety import _manual_settings

    topology = mono_output_topology(card_id=None)
    _set_active_speaker_state_paths(monkeypatch, tmp_path)

    save_output_topology(topology)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    candidate = {
        "between_roles": ["woofer", "tweeter"],
        "frequency_hz": 5500,
        "filter_type": "Linkwitz-Riley",
        "confidence": "medium",
    }
    if slope_db_per_octave is not None:
        candidate["slope_db_per_octave"] = slope_db_per_octave
    manual = _manual_settings()
    manual["crossover_candidates"] = [candidate]

    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))
    saved = sound_setup._active_speaker_design_draft_save_payload({
        "manual_settings": manual,
        "operator_inputs": operator_inputs or {},
    })
    assert fsync_calls == []  # ordinary wizard save: no fsync
    assert saved["driver_safety_profile"]["issues"] == []
    return fsync_calls


def _geometry(fc_hz: float, slope_db_per_octave: int):
    from jasper.active_speaker.crossover_declaration import CrossoverGeometry

    return CrossoverGeometry(
        fc_hz=fc_hz,
        filter_type="Linkwitz-Riley",
        slope_db_per_octave=slope_db_per_octave,
    )


def test_measured_fc_saves_the_declaration_and_leaves_the_loop_open(
    monkeypatch, tmp_path: Path,
) -> None:
    from jasper.active_speaker.design_draft import load_design_draft

    _declared_candidate_box(
        monkeypatch, tmp_path, operator_inputs={"notes": "keep this"}
    )

    saved = sound_setup.apply_measured_crossover_geometry(
        between_roles=("woofer", "tweeter"),
        configured=_geometry(5500, 24),
        selected=_geometry(5750, 24),
    )

    assert saved["revision"] == 2
    assert saved["operator_inputs"]["notes"] == "keep this"
    # The whole point: the machine's own write does not lock the loop it is
    # part of. Also asserted on the RELOADED artifact, since the gates read from
    # disk rather than from this return value.
    profile = saved["driver_safety_profile"]
    assert saved["driver_safety_profile"]["issues"] == []
    reloaded = load_design_draft(topology=sound_active_speaker.load_output_topology())
    assert reloaded["driver_safety_profile"] == profile


@pytest.mark.parametrize(
    ("selected_fc_hz", "selected_slope"),
    [
        pytest.param(5750, 24, id="frequency_only"),
        # The declaration is three fields, not one: a candidate re-measured at
        # the SAME corner but a different slope must still write. "The frequency
        # didn't move" is not license to treat the accept as a no-op.
        pytest.param(5500, 48, id="slope_only"),
        pytest.param(6000, 48, id="frequency_and_slope"),
    ],
)
def test_apply_measured_crossover_geometry_writes_the_measured_declaration(
    monkeypatch, tmp_path: Path, selected_fc_hz, selected_slope,
) -> None:
    """The crossover-accept seam lands every moved field and fsyncs its
    design-draft write, file and directory (#2292)."""
    fsync_calls = _declared_candidate_box(monkeypatch, tmp_path)

    saved = sound_setup.apply_measured_crossover_geometry(
        between_roles=("woofer", "tweeter"),
        configured=_geometry(5500, 24),
        selected=_geometry(selected_fc_hz, selected_slope),
    )

    candidate = saved["manual_settings"]["crossover_candidates"][0]
    assert candidate["frequency_hz"] == selected_fc_hz
    assert candidate["slope_db_per_octave"] == selected_slope
    assert len(fsync_calls) == 2  # crossover-accept seam: file fsync + dir fsync


@pytest.mark.parametrize(
    "declared_slope",
    [
        # The live slope no longer matches what ``configured`` claims, even
        # though the corner itself still lines up: the CAS binds on all three
        # declared fields, not just ``frequency_hz``.
        pytest.param(24, id="declared_slope_moved"),
        # A declared candidate carrying no slope at all cannot be reconciled
        # into a CrossoverGeometry, so the writer refuses rather than completing
        # the missing field with the caller's own guess — the same refusal as a
        # genuine mismatch, not a silent fill-in.
        pytest.param(None, id="declared_slope_absent"),
    ],
)
def test_apply_measured_crossover_geometry_refuses_an_unreconcilable_declaration(
    monkeypatch, tmp_path: Path, declared_slope,
) -> None:
    _declared_candidate_box(
        monkeypatch, tmp_path, slope_db_per_octave=declared_slope
    )

    with pytest.raises(
        ValueError, match="Sound changed since this measurement; review afresh"
    ):
        sound_setup.apply_measured_crossover_geometry(
            between_roles=("woofer", "tweeter"),
            configured=_geometry(5500, 48),
            selected=_geometry(5500, 24),
        )


LEFT_APPLE_SERIAL = "DWH53530FHL2FN3AC"
RIGHT_APPLE_SERIAL = "DWH53530FLL2FN3A3"


def _apple_cards(*serials: str) -> list[OutputCardFact]:
    """The commissioned pair's cards, one per serial, on their fixed USB ports."""

    return [
        OutputCardFact(
            card_id=card_id,
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial=serial,
            usb_path=usb_path,
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
        )
        for card_id, usb_path, serial in zip(
            ("A", "A_1"), ("usb1/1-2", "usb1/1-1"), serials
        )
    ]


def _observe_apple_cards(tmp_path: Path, *serials: str) -> None:
    write_output_hardware_state(
        classify_output_cards(_apple_cards(*serials)),
        path=tmp_path / "output_hardware.json",
    )


def _dual_apple_hardware() -> dict:
    return {
        "device_id": DUAL_APPLE_ACTIVE_DEVICE_ID,
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left_dac",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": LEFT_APPLE_SERIAL,
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right_dac",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": RIGHT_APPLE_SERIAL,
                "physical_output_indexes": [2, 3],
            },
        ],
    }


def _dual_apple_stereo_topology_raw(*, identity_verified: bool = True) -> dict:
    """The commissioned dual-Apple stereo active pair, one cabinet per dongle."""

    def group(group_id: str, kind: str, woofer: int, tweeter: int) -> dict:
        woofer_channel: dict = {"role": "woofer", "physical_output_index": woofer}
        tweeter_channel: dict = {
            "role": "tweeter",
            "physical_output_index": tweeter,
            "startup_muted": True,
            "protection_required": True,
        }
        if identity_verified:
            woofer_channel["identity_verified"] = True
            tweeter_channel["identity_verified"] = True
        return {
            "id": group_id,
            "label": f"{group_id.title()} speaker",
            "kind": kind,
            "mode": "active_2_way",
            "channels": [woofer_channel, tweeter_channel],
        }

    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple_pair",
        "name": "Dual Apple stereo active pair",
        "status": "draft",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [
            group("left", "left", 0, 1),
            group("right", "right", 2, 3),
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
        },
    }


def test_sound_output_topology_payload_uses_observed_dual_apple_hardware_state(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    _observe_apple_cards(tmp_path, LEFT_APPLE_SERIAL, RIGHT_APPLE_SERIAL)

    envelope = sound_setup._output_topology_payload()
    payload = envelope["output_topology"]

    assert payload["hardware"]["device_id"] == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert payload["hardware"]["device_label"] == "Dual Apple USB-C DAC 4-channel pair"
    assert payload["hardware"]["physical_output_count"] == 4
    assert payload["hardware"]["child_devices"][0]["serial"] == LEFT_APPLE_SERIAL
    assert envelope["clock_domain"]["status"] == "dual_apple_composite_clock"
    assert envelope["clock_domain"]["composite_clock_supported"] is True
    assert payload["safety"]["sound_tests_allowed"] is False


@pytest.mark.parametrize(
    ("observed", "observed_profile_id", "observed_output_count", "issue"),
    [
        pytest.param(
            (LEFT_APPLE_SERIAL,),
            APPLE_USB_C_DONGLE_DEVICE_ID,
            2,
            "dual_apple_observed_profile_mismatch",
            id="one_unit_attached",
        ),
        pytest.param(
            ("WRONGLEFTSERIAL", "WRONGRIGHTSERIAL"),
            DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
            4,
            "dual_apple_observed_serial_mismatch",
            id="wrong_pair_attached",
        ),
    ],
)
def test_a_saved_dual_apple_pair_blocks_its_clock_on_the_hardware_it_observes(
    monkeypatch,
    tmp_path: Path,
    observed,
    observed_profile_id,
    observed_output_count,
    issue,
):
    """The saved 4-channel shape survives; the composite clock does not.

    The read reports both sides separately — what was commissioned under
    ``output_topology`` and what is attached under ``output_hardware`` — so a
    swapped or half-attached pair blocks the composite clock by name instead of
    silently adopting whatever is plugged in.
    """
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    _observe_apple_cards(tmp_path, LEFT_APPLE_SERIAL, RIGHT_APPLE_SERIAL)
    sound_setup._save_output_topology_payload(_dual_apple_stereo_topology_raw())
    _observe_apple_cards(tmp_path, *observed)

    envelope = sound_setup._output_topology_payload()
    payload = envelope["output_topology"]
    clock = envelope["clock_domain"]

    assert payload["hardware"]["device_id"] == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert payload["hardware"]["physical_output_count"] == 4
    assert envelope["output_hardware"]["status"] == "ready"
    assert envelope["output_hardware"]["profile_id"] == observed_profile_id
    assert envelope["output_hardware"]["physical_output_count"] == (
        observed_output_count
    )
    assert clock["status"] == "dual_apple_composite_clock_blocked"
    assert issue in {entry["code"] for entry in clock["issues"]}
    assert clock["composite_clock_supported"] is False
    assert clock["coherent_physical_output_count"] == 0


def test_sound_output_topology_save_accepts_measured_dual_apple_hardware(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    _observe_apple_cards(tmp_path, LEFT_APPLE_SERIAL, RIGHT_APPLE_SERIAL)

    sound_setup._save_output_topology_payload(
        _dual_apple_stereo_topology_raw(identity_verified=False)
    )
    payload = sound_setup._output_topology_payload()

    topology = payload["output_topology"]

    assert topology["status"] == "valid"
    assert topology["hardware"]["physical_output_count"] == 4
    assert payload["clock_domain"]["status"] == "dual_apple_composite_clock"
    assert payload["clock_domain"]["composite_clock_supported"] is True
    assert payload["clock_domain"]["multi_device_aggregate_supported"] is False
    assert topology["safety"]["sound_tests_allowed"] is False


def test_sound_output_topology_save_accepts_a_cross_child_speaker_group(
    monkeypatch,
    tmp_path: Path,
):
    """The save door persists a layout its own evaluation warns about.

    One cabinet with its woofer on dongle A and its tweeter on dongle B puts an
    uncorrected clock seam inside a crossover. That is a fidelity cost, not a
    hearing-safety one, so per the never-nanny ruling the save is ACCEPTED. The
    verdict's own shape is pinned where it is built, in test_output_topology.py.
    """

    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    _observe_apple_cards(tmp_path, LEFT_APPLE_SERIAL, RIGHT_APPLE_SERIAL)

    sound_setup._save_output_topology_payload({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple_pair",
        "name": "Dual Apple cross-child mono",
        "status": "draft",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        # Output 1 belongs to the left dongle...
                        "physical_output_index": 0,
                    },
                    {
                        "role": "tweeter",
                        # ...and output 3 belongs to the right one.
                        "physical_output_index": 2,
                        "startup_muted": True,
                        "protection_required": True,
                    },
                ],
            },
        ],
        "routing": {"mono_group_id": "mono"},
    })

    topology = sound_setup._output_topology_payload()["output_topology"]

    # Accepted, not refused: it persisted and it is not blocked.
    assert path.exists()
    assert topology["status"] == "valid"
    assert topology["safety"]["blockers"] == []


def test_sound_output_topology_save_validates_and_persists_complete_contract(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(
        {"output_topology": _passive_left_topology_payload()}
    )
    payload = sound_setup._output_topology_payload()
    topology = payload["output_topology"]
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert topology["status"] == "valid"
    assert topology["evaluation"]["assigned_output_count"] == 1
    assert topology["safety"]["sound_tests_allowed"] is False
    assert saved["status"] == "valid"
    assert saved["speaker_groups"][0]["channels"][0]["human_output_label"] == (
        "DAC output 1"
    )
    assert payload["clock_domain"]["status"] == "single_device_clock"


def test_sound_output_topology_http_route_is_csrf_protected_and_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    _record_dac8x()
    with sound_server(tmp_path) as base:
        get_resp = urllib.request.urlopen(f"{base}/output-topology")
        get_payload = json.loads(get_resp.read().decode("utf-8"))
        assert get_payload["output_topology"]["status"] == "draft"

        post_resp = request_with_csrf(
            base,
            "/output-topology",
            json.dumps({
                "output_topology": get_payload["output_topology"],
            }).encode("utf-8"),
            content_type="application/json",
        )
        post_payload = json.loads(post_resp.read().decode("utf-8"))
        assert post_payload["output_topology"]["safety"]["sound_tests_allowed"] is False


def test_sound_output_topology_reset_http_route_is_csrf_protected(
    monkeypatch,
    tmp_path: Path,
):
    calls = []
    monkeypatch.setattr(
        sound_setup,
        "_reset_output_topology_payload",
        lambda raw: calls.append(raw) or {"output_topology": {"status": "draft"}},
    )
    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(base, "/output-topology/reset", {})
        payload = json.loads(resp.read().decode("utf-8"))

        assert calls == [{}]
        assert payload["output_topology"]["status"] == "draft"


def _apple_dongle_detected() -> None:
    write_output_hardware_state(OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
    ))


def _dac8x_detected() -> None:
    write_output_hardware_state(OutputHardwareState(
        profile_id="hifiberry_dac8x",
        profile_label="HiFiBerry DAC8x",
        status="ready",
        physical_output_count=8,
    ))


def test_reset_adopts_hardware_read_after_parking(monkeypatch):
    save_output_topology(OutputTopology.from_mapping(_passive_left_topology_payload()))
    _apple_dongle_detected()
    read_hardware = sound_active_speaker.load_output_hardware_state
    save = OutputTopologyMutation.save
    events = []

    def assert_transaction_held():
        with pytest.raises(TimeoutError):
            with output_topology_mutation(timeout_sec=0):
                pass

    def checked_read():
        assert_transaction_held()
        events.append("read")
        return read_hardware()

    def checked_save(self, topology):
        assert_transaction_held()
        assert topology.speaker_groups == ()
        events.append("write")
        return save(self, topology)

    def checked_clear():
        assert_transaction_held()
        events.append("clear")
        return {"status": "cleared"}

    def park_and_commit(_topology, commit, **_kwargs):
        _dac8x_detected()
        with monkeypatch.context() as patch:
            patch.setattr(sound_active_speaker, "load_output_hardware_state", checked_read)
            return _RuntimeMutation(commit())

    monkeypatch.setattr(OutputTopologyMutation, "save", checked_save)
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )
    _stub_audio_stops(monkeypatch)
    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state", checked_clear,
    )
    _stub_reconcile(monkeypatch, {"ok": True})

    result = sound_setup._reset_output_topology_payload({})

    assert events == ["read", "write", "clear"]
    assert result["reset"]["status"] == "reset"
    assert load_output_topology().hardware.device_id == "hifiberry_dac8x"
    assert load_output_topology().speaker_groups == ()


def test_reset_after_a_normal_save_clears_the_layout(
    monkeypatch, tmp_path: Path,
):
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH", str(tmp_path / "staged.yml")
    )
    _stub_audio_stops(monkeypatch)
    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        lambda: {"status": "cleared", "removed": []},
    )

    sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )
    reset = sound_setup._reset_output_topology_payload({})

    assert reset["saved"] is True
    assert reset["output_topology"]["speaker_groups"] == []


def test_reset_http_reports_ambiguous_failure_with_current_topology(
    monkeypatch, tmp_path: Path,
) -> None:
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    def publish_then_fail(_topology, commit, **_kwargs):
        commit()
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        publish_then_fail,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        lambda: {"status": "cleared", "removed": []},
    )
    _stub_audio_stops(monkeypatch)

    with sound_server(tmp_path) as base:
        response = json_post_with_csrf(
            base,
            "/output-topology/reset",
            {},
            expect_status=502,
        )
        payload = json.loads(response.read().decode("utf-8"))

    assert payload["reset"]["status"] == "needs_attention"
    assert payload["output_topology"]["speaker_groups"] == []


#: The seven artifacts ``clear_active_speaker_setup_state`` unlinks, by env var.
_RESET_UNLINKED_STATE_ENVS = (
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
    "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE",
    "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
)


def _stale_setup_state(
    monkeypatch, tmp_path: Path, *, outputd_ok: bool,
) -> tuple[list[Path], list[str]]:
    """Stale setup state on disk, and the ordered log one reset writes into."""
    from jasper.active_speaker.reset import clear_active_speaker_setup_state

    paths = _set_active_speaker_state_paths(
        monkeypatch,
        tmp_path,
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH",
        *_RESET_UNLINKED_STATE_ENVS,
    )
    written = [paths[name] for name in _RESET_UNLINKED_STATE_ENVS]
    for path in written:
        path.write_text('{"stale": true}\n', encoding="utf-8")

    events = _stub_audio_stops(monkeypatch)

    def stop_outputd(*_units, **_kwargs):
        events.append("outputd-stop")
        return {"ok": outputd_ok}

    def clear_and_record():
        events.append("clear")
        return clear_active_speaker_setup_state()

    monkeypatch.setattr("jasper.control.restart_broker.manage_units", stop_outputd)
    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        clear_and_record,
    )
    _stub_reconcile(monkeypatch, {"ok": True})
    return written, events


def test_reset_stops_audio_before_it_unlinks_setup_state(monkeypatch, tmp_path: Path):
    """Unlinking first would leave the speaker playing a graph whose own record
    is already gone, with nothing left to say what it is playing."""
    paths, events = _stale_setup_state(monkeypatch, tmp_path, outputd_ok=True)

    payload = sound_setup._reset_output_topology_payload({})

    assert events == ["safe", "outputd-stop", "clear"]
    assert payload["reset"]["status"] == "reset"
    assert [path for path in paths if path.exists()] == []


def test_reset_unlinks_nothing_when_the_audio_stop_fails(monkeypatch, tmp_path: Path):
    paths, events = _stale_setup_state(monkeypatch, tmp_path, outputd_ok=False)

    with pytest.raises(RuntimeError):
        sound_setup._reset_output_topology_payload({})

    assert events == ["safe", "outputd-stop"]
    assert all(path.exists() for path in paths)


def test_reset_cleanup_failure_keeps_new_topology_and_does_not_restore_old_graph(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(new_topology_draft(name="Old intent"), topology_path)
    events: list[str] = []

    def park_and_commit(_topology, commit, **_kwargs):
        events.append("park")
        committed = commit()
        events.append("converge-new-graph")
        return _RuntimeMutation(committed)

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )

    def fail_cleanup():
        events.append("cleanup")
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        fail_cleanup,
    )
    monkeypatch.setattr(
        "jasper.output_topology_runtime.trigger_reconcile",
        lambda **_kwargs: {"ok": True},
    )
    _stub_audio_stops(monkeypatch)

    payload = sound_setup._reset_output_topology_payload({})

    assert events == ["park", "cleanup", "converge-new-graph"]
    assert load_output_topology(topology_path).speaker_groups == ()
    assert payload["reset"]["status"] == "needs_attention"


@pytest.mark.parametrize(
    ("reconcile", "status"),
    [
        (RECONCILE_STILL_CONVERGING, "converging"),
        (RECONCILE_FAILED, "needs_attention"),
    ],
)
def test_reset_reports_the_reconcile_verdict(
    monkeypatch, tmp_path: Path, reconcile, status,
) -> None:
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(new_topology_draft(name="Old intent"), topology_path)
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda _topology, commit, **_kwargs: _RuntimeMutation(commit()),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        lambda: {"status": "cleared"},
    )
    _stub_reconcile(monkeypatch, reconcile)
    _stub_audio_stops(monkeypatch)

    payload = sound_setup._reset_output_topology_payload({})

    assert payload["reset"]["status"] == status


def _bank_rear_calibration_applied_fixture(monkeypatch, tmp_path: Path) -> dict:
    """An applied baseline on a rear-output topology, so ``--base saved``
    resolves — the shape ``candidate_from_applied_profile`` needs."""
    from jasper.active_speaker import baseline_profile as baseline_profile_mod
    from .active_speaker_fixtures import declared_graph_fixture, standard_design_draft
    from .test_rear_output_foundation import _rear_pair

    _set_active_speaker_state_paths(
        monkeypatch, tmp_path, "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.bundles.sessions_dir", lambda: tmp_path / "sessions",
    )
    _, topology = _rear_pair("mono")
    save_output_topology(topology, path=Path(os.environ["JASPER_OUTPUT_TOPOLOGY_PATH"]))
    draft = standard_design_draft(topology)
    declaration, declared = declared_graph_fixture(topology, draft)
    prepared = baseline_profile_mod.prepare_applied_baseline_profile(
        bank_candidate(declared), declaration=declaration, design_draft=draft,
        config_path=None, config_sha256="",
    )
    prepared["status"] = "applied"
    state_path = Path(os.environ["JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE"])
    state_path.write_text(json.dumps(prepared), encoding="utf-8")
    return prepared


def test_rear_calibration_seed_route_returns_a_document_that_validates(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json"))
    with sound_server(tmp_path) as base:
        seed_resp = urllib.request.urlopen(f"{base}/active-speaker/rear-calibration/seed")
        seed_payload = json.loads(seed_resp.read().decode("utf-8"))
        assert seed_payload["ok"] is True
        assert seed_payload["calibration"]["kind"] == "jts_rear_calibration"

        validate_resp = request_with_csrf(
            base, "/active-speaker/rear-calibration/validate",
            json.dumps(seed_payload["calibration"]).encode("utf-8"),
            content_type="application/json",
        )
        validate_payload = json.loads(validate_resp.read().decode("utf-8"))

    assert validate_payload == {
        "ok": True, "case": "electrical_dsp", "summary": "muted electrical rear stage",
    }


def test_rear_calibration_validate_route_refuses_a_bad_document_with_its_code(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json"))
    with sound_server(tmp_path) as base:
        bad_document = {**sound_active_speaker._active_speaker_rear_calibration_seed_payload()["calibration"],
                        "sample_rate_hz": 44100}
        resp = request_with_csrf(
            base, "/active-speaker/rear-calibration/validate",
            json.dumps(bad_document).encode("utf-8"),
            content_type="application/json",
        )
        payload = json.loads(resp.read().decode("utf-8"))

    assert payload["ok"] is False
    assert payload["code"] == "rear_calibration_invalid"
    assert payload["next_action"] is None
    assert isinstance(payload["error"], str) and payload["error"]


def test_rear_calibration_bank_route_banks_without_touching_the_applied_identity(
    monkeypatch, tmp_path: Path,
):
    prepared = _bank_rear_calibration_applied_fixture(monkeypatch, tmp_path)
    base_fingerprint = prepared["source"]["measured_candidate_fingerprint"]
    document = {
        **sound_active_speaker._active_speaker_rear_calibration_seed_payload()["calibration"],
        "rear_muted": False,
    }
    with sound_server(tmp_path) as base:
        resp = request_with_csrf(
            base, "/active-speaker/rear-calibration/bank",
            json.dumps(document).encode("utf-8"),
            content_type="application/json",
        )
        payload = json.loads(resp.read().decode("utf-8"))

    assert payload["ok"] is True
    assert payload["candidate_fingerprint"] != base_fingerprint
    assert payload["issues"] == []
    state_path = Path(os.environ["JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE"])
    reloaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert reloaded["source"]["measured_candidate_fingerprint"] == base_fingerprint


def test_rear_calibration_bank_route_names_the_section_on_a_refused_document(
    monkeypatch, tmp_path: Path,
):
    from .test_active_speaker_measured_crossover_candidate import _acoustic_rear_document

    _bank_rear_calibration_applied_fixture(monkeypatch, tmp_path)
    with sound_server(tmp_path) as base:
        resp = request_with_csrf(
            base, "/active-speaker/rear-calibration/bank",
            json.dumps(_acoustic_rear_document()).encode("utf-8"),
            content_type="application/json",
        )
        payload = json.loads(resp.read().decode("utf-8"))

    assert payload["ok"] is False
    assert payload["code"] == "rear_calibration_case_unsupported"
    assert payload["section"] == "rear_calibration"


def test_rear_calibration_bank_route_refuses_a_corrupt_saved_topology(
    monkeypatch, tmp_path: Path,
):
    """Mirrors jasper-crossover-prescriber's ``--base saved`` block: a corrupt
    on-disk file fails closed as a typed refusal, not an unhandled 502."""

    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json"))
    monkeypatch.setattr(
        "jasper.output_topology_store.load_output_topology_strict",
        lambda *a, **kw: (_ for _ in ()).throw(OutputTopologyError("output topology is not valid JSON")),
    )
    document = sound_active_speaker._active_speaker_rear_calibration_seed_payload()["calibration"]
    with sound_server(tmp_path) as base:
        resp = request_with_csrf(
            base, "/active-speaker/rear-calibration/bank",
            json.dumps(document).encode("utf-8"),
            content_type="application/json",
        )
        payload = json.loads(resp.read().decode("utf-8"))

    assert payload["ok"] is False
    assert payload["code"] == "evidence_unreadable"
    assert payload["section"] is None


def test_active_speaker_baseline_http_route_is_exposed(
    monkeypatch,
    tmp_path: Path,
):
    _set_active_speaker_state_paths(
        monkeypatch,
        tmp_path,
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
    )
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "hifiberry_dac8x")
    with sound_server(tmp_path) as base:
        profile_resp = urllib.request.urlopen(
            f"{base}/active-speaker/baseline-profile"
        )
        profile_payload = json.loads(profile_resp.read().decode("utf-8"))

        assert profile_payload["kind"] == "jts_active_speaker_baseline_profile_candidate"
        assert profile_payload["permissions"]["may_apply"] is False


BASELINE_CONFIG_PATH = "/var/lib/camilladsp/configs/active_speaker_baseline.yml"


def _stub_baseline_apply(
    monkeypatch, *, applied_profile: bool = True, refusal: dict | None = None,
):
    """Stub the graph apply and collect finish cleanup/source restoration.

    ``refusal`` is returned verbatim instead of an applied envelope, and the
    verification callback stays unrun — nothing was verified.
    """

    apply_calls: list[dict] = []
    mux_commands: list[str] = []

    async def fake_apply_candidate(candidate=None, **kwargs):
        apply_calls.append(kwargs)
        if refusal is not None:
            return refusal
        callback = kwargs.get("on_candidate_verified")
        if callback is not None:
            await callback()
        applied = {
            "status": "applied",
            "apply": {"result": "success", "active_config_path": BASELINE_CONFIG_PATH},
            "issues": [],
        }
        if applied_profile:
            applied["profile"] = {
                "status": "applied",
                "config": {
                    "path": BASELINE_CONFIG_PATH,
                    "basename": "active_speaker_baseline.yml",
                },
                "permissions": {"may_apply": False},
                "issues": [],
            }
        return applied

    async def fake_mux_command(command: str) -> dict:
        mux_commands.append(command)
        return {
            "mode": "auto",
            "selected_source": None,
            "active_source": "airplay",
            "test_source": None,
        }

    monkeypatch.setattr(
        "jasper.web.correction_crossover_v2_apply.apply_candidate",
        fake_apply_candidate,
    )
    monkeypatch.setattr(
        sound_active_speaker, "mux_socket_command", fake_mux_command
    )
    monkeypatch.setattr(sound_active_speaker, "trigger_reconcile", lambda **kw: {"ok": True})
    return apply_calls, mux_commands


@pytest.mark.parametrize("route_ready", [True, False])
async def test_active_speaker_baseline_apply_converges_route_before_source_auto(monkeypatch, route_ready):
    apply_calls, mux_commands = _stub_baseline_apply(
        monkeypatch, applied_profile=False
    )
    def reconcile(**kwargs):
        assert len(apply_calls) == 1
        assert mux_commands == []
        return {"ok": route_ready}
    monkeypatch.setattr(sound_active_speaker, "trigger_reconcile", reconcile)

    payload = await sound_active_speaker._active_speaker_baseline_profile_apply_payload(
        camilla_factory=lambda: FakeCamilla("/tmp/prior.yml"),
    )

    assert payload["reconcile"]["ok"] is route_ready
    assert mux_commands == (["AUTO"] if route_ready else [])
    assert payload["status"] == ("applied" if route_ready else "needs_attention")
    if route_ready:
        assert payload["source_selection_restore"]["state"]["mode"] == "auto"
    else:
        assert payload["issues"][-1]["code"] == "output_route_not_ready"


@pytest.mark.parametrize("echo", [None, "stale-candidate", "reviewed-candidate"])
def test_active_speaker_finish_commissioning_ignores_page_echo(monkeypatch, tmp_path, echo):
    apply_calls, mux_commands = _stub_baseline_apply(monkeypatch)

    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    body = json.dumps({"expected_candidate_fingerprint": echo}).encode()
    response, _ = _drive_raw_sound_post(
        tmp_path, path="/active-speaker/baseline-profile/save-and-apply",
        body=body, content_length=len(body),
    )
    assert response.startswith(b"HTTP/1.1 200")
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])

    assert len(apply_calls) == 1
    assert mux_commands == ["AUTO"]
    assert payload["status"] == "applied"
    assert payload["profile"]["status"] == "applied"
    assert payload["source_selection_restore"]["status"] == "ok"
    assert payload["output_safety"] == {
        "safety_muted": False,
        "reason": None,
        "active_config_path": BASELINE_CONFIG_PATH,
    }


def test_save_and_apply_answers_a_refusal_as_a_typed_two_hundred(monkeypatch, tmp_path):
    """A refused finish is a 200 carrying the refusal, never an HTTP error.

    ``jasper/cli/basic_profile.py`` reads any non-200 as an unreachable door, so
    the status is a wire contract only HTTP can pin. When the door refuses is
    pinned at module altitude in test_cli_seat_level.py.
    """
    _, mux_commands = _stub_baseline_apply(monkeypatch, refusal={
        "status": "blocked",
        "issues": [{
            "severity": "blocker",
            "code": "baseline_config_validation_failed",
            "message": "x",
        }],
    })
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)

    response, _ = _drive_raw_sound_post(
        tmp_path, path="/active-speaker/baseline-profile/save-and-apply",
        body=b"{}", content_length=2,
    )

    assert response.startswith(b"HTTP/1.1 200")
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert payload["status"] == "blocked"
    assert [issue["code"] for issue in payload["issues"]] == [
        "baseline_config_validation_failed"
    ]
    assert mux_commands == []


async def test_active_speaker_finish_proof_refusal_skips_cleanup(monkeypatch):
    seen = {}

    async def refuse_after_locked_refresh(**kwargs):
        seen.update(kwargs)
        return {
            "status": "blocked",
            "profile": {"candidate_fingerprint": "newer-candidate"},
            "apply": None,
            "issues": [
                {"code": "baseline_graph_safety_proof_failed"}
            ],
        }

    monkeypatch.setattr(
        sound_active_speaker,
        "_active_speaker_baseline_profile_apply_payload",
        refuse_after_locked_refresh,
    )

    payload = await sound_setup._active_speaker_finish_commissioning_payload(
        camilla_factory=lambda: pytest.fail("refused apply must not open CamillaDSP"),
    )

    assert callable(seen["on_candidate_verified"])
    assert payload["status"] == "blocked"
    assert payload["commissioning_cleanup"] == {"status": "not_attempted"}


async def test_active_speaker_finish_commissioning_clears_pending_ramp(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.active_speaker.commission_ramp import (
        _ramp_base_state,
        _record_ramp_state,
        load_ramp_state,
        ramp_state_path,
    )

    for name, filename in (
        ("JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE", "ramp.json"),
        ("JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE", "commission-load.json"),
        ("JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", "safe-playback.json"),
    ):
        monkeypatch.setenv(name, str(tmp_path / filename))
    _record_ramp_state({
        **_ramp_base_state(ramp_state_path()),
        "speaker_group_id": "main",
        "confirmed_roles": ["woofer"],
        "pending": {
            "role": "tweeter",
            "gain_db": -20.0,
            "playback_id": "late-auto-ramp-step",
        },
        "last_action": "late_step",
    })
    _stub_baseline_apply(monkeypatch)

    payload = await sound_setup._active_speaker_finish_commissioning_payload(
        camilla_factory=lambda: FakeCamilla("/tmp/prior.yml"),
    )

    assert payload["status"] == "applied"
    assert payload["commissioning_cleanup"]["ramp"]["status"] == "aborted"
    assert load_ramp_state()["pending"] is None


def test_active_speaker_crossover_preview_get_tracks_draft_without_preview_file(
    monkeypatch, tmp_path: Path,
):
    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)
    sound_setup._save_output_topology_payload(_active_speaker_mono_topology_payload(
        card_id=None, identity_verified=True,
    ))
    _save_active_speaker_design_and_preview()
    handler_cls = sound_setup._make_handler(
        profile_path=tmp_path / "profile.json", library_path=tmp_path / "library.json",
        config_dir=tmp_path / "configs",
    )
    for frequency in (2500, 3200):
        draft_path = paths["JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"]
        draft = json.loads(draft_path.read_text())
        draft["driver_research"]["crossover_candidates"][0]["frequency_hz"] = frequency
        draft_path.write_text(json.dumps(draft))
        handler = handler_cls.__new__(handler_cls)
        handler.rfile = io.BytesIO(b"GET /active-speaker/crossover-preview HTTP/1.1\r\nHost: jts.local\r\n\r\n")
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 0)
        handler.server = None
        handler.raw_requestline = handler.rfile.readline()
        handler.parse_request()
        handler.do_GET()
        response = handler.wfile.getvalue()
        assert b" 200 " in response.split(b"\r\n", 1)[0]
        payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
        assert payload["status"] == "ready_for_protected_staging"
        assert payload["groups"][0]["crossovers"][0]["proposed_frequency_hz"] == frequency
        assert payload["safety"]["no_audio"] is True
        assert payload["safety"]["emits_camilla_yaml"] is False
        assert "design_draft_fingerprint" not in payload["source"]
        assert "preview_fingerprint" not in payload["source"]


@pytest.fixture(scope="module")
def sound_harness_out() -> dict:
    """Run the /sound/ JS harness once and hand every scenario its verdict."""

    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_SOUND_HARNESS), str(_SOUND_MODULE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_sound_module_replays_latest_tab_intent_after_apply_finishes(
    sound_harness_out,
):
    assert sound_harness_out["applyProfileIds"] == ["stock:flat"]
    assert sound_harness_out["liveDraftRequests"] == 1
    assert sound_harness_out["liveDraftEpoch"] == "apply-1"
    assert sound_harness_out["liveTabMarked"] is True


@pytest.mark.parametrize("scenario", [
    "blockedEqCarrierIsThePageState", "splitPageModesRenderAndBootOnlyOwnedSurfaces",
    "eqSliderDragSendsNoLiveAudioUntilRelease", "volumeFloorRequiresExplicitSaveButAuditionsDraft",
])
def test_the_sound_module_passes_its_harness_scenario(sound_harness_out, scenario):
    assert {scenario: True} in sound_harness_out["results"]


def test_state_payload_contains_stock_curves_profiles_and_preview(tmp_path: Path):
    payload = sound_setup._state_payload(
        SoundProfile(curve_id="harman"),
        library_path=tmp_path / "sound_profiles.json",
        include_library=True,
    )

    assert [curve["id"] for curve in payload["curves"]] == ["flat", "harman", "bk"]
    assert [entry["id"] for entry in payload["profile_library"][:3]] == [
        "stock:flat",
        "stock:harman",
        "stock:bk",
    ]
    assert payload["profile"]["curve_id"] == "harman"
    assert payload["preview"]
    assert "components" not in payload  # single-line graph: no per-band overlay data
    assert payload["limits"]["max_parametric_bands"] == 8
    # Cut-filter Q ceiling is exposed so the UI's Width slider can bound HP/LP.
    assert payload["limits"]["cut_max_q"] == 1.4
    # Volume-floor SSOT: the reset/default floor is forwarded from the one
    # backend owner (volume_curve.DEFAULT_VOLUME_FLOOR_DB, re-exported via
    # sound.settings) so the /sound/ editor stops hardcoding -50. If this drifts,
    # the page's reset button + default would silently disagree with the server.
    assert payload["limits"]["volume_floor_default_db"] == DEFAULT_VOLUME_FLOOR_DB
    assert payload["headroom_db"] > 0


def test_state_filter_count_signals_effective_eq_for_initial_view():
    # filter_count drives the page's initial Off-vs-Saved tab: 0 means no
    # effective EQ (bypassed OR flat) -> open Off; >0 -> open Saved with the
    # applied profile marked active.
    assert sound_setup._state_payload(SoundProfile())["filter_count"] == 0
    assert sound_setup._state_payload(
        SoundProfile(enabled=False, curve_id="harman")
    )["filter_count"] == 0
    assert sound_setup._state_payload(
        SoundProfile(curve_id="harman")
    )["filter_count"] > 0
    assert sound_setup._state_payload(
        SoundProfile(simple_eq=SimpleEq(bass_db=3.0))
    )["filter_count"] > 0
    # A cuts-only EQ has zero headroom but is still an effective EQ -- this is
    # why the signal is filter_count, not headroom_db.
    cuts_only = sound_setup._state_payload(SoundProfile(simple_eq=SimpleEq(mid_db=-3.0)))
    assert cuts_only["headroom_db"] == 0
    assert cuts_only["filter_count"] > 0


@pytest.mark.parametrize(
    "config_name, layout, expected",
    [
        ("foreign.yml", True, {"status": "blocked", "reason_code": "unknown_config"}),
        ("sound_current.yml", True, {"status": "ok"}),
        (
            "sound_current.yml",
            False,
            {"status": "blocked", "reason_code": FLAT_PROGRAM_GRAPH_UNCONFIGURED},
        ),
    ],
)
def test_state_reports_whether_the_loaded_graph_can_host_eq(
    tmp_path: Path, monkeypatch, config_name: str, layout: bool, expected: dict,
):
    """/sound/eq/ opens on the refusal instead of discovering it at save time,
    so /state says whether the LOADED graph can carry preference EQ."""
    import jasper.camilla

    if layout:
        _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    else:
        monkeypatch.setenv(
            "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "no_topology.json"),
        )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / config_name
    current.write_text(
        "devices: {}\n" if config_name == "foreign.yml" else _room_config()
    )
    monkeypatch.setattr(
        jasper.camilla, "primary_controller", lambda: FakeCamilla(str(current)),
    )

    with sound_server(tmp_path) as base:
        payload = json.loads(
            urllib.request.urlopen(f"{base}/state").read().decode("utf-8")
        )

    carrier = payload["eq_carrier"]
    assert {k: carrier[k] for k in expected} == expected
    if carrier["status"] == "blocked":
        assert carrier["message"]


@pytest.mark.parametrize("failure", ["no_loaded_path", "controller_unreachable"])
def test_state_falls_open_when_camilla_cannot_be_read(
    tmp_path: Path, monkeypatch, failure: str,
):
    """The POST refusal is the fail-closed gate; an unreachable CamillaDSP must
    not blank the editor."""
    import jasper.camilla

    class _Unreachable:
        async def get_config_file_path(self, *, best_effort: bool = False):
            if failure == "controller_unreachable":
                raise RuntimeError("CamillaDSP websocket is not answering")
            return None

    monkeypatch.setattr(jasper.camilla, "primary_controller", _Unreachable)

    with sound_server(tmp_path) as base:
        with urllib.request.urlopen(f"{base}/state") as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))

    assert payload["eq_carrier"] == {"status": "unknown"}


@pytest.mark.parametrize("header", ["speaker", "output"])
def test_state_skips_the_carrier_probe_off_the_eq_page(
    tmp_path: Path, monkeypatch, header,
):
    """The probe is a dry-run recompose of the loaded graph. Only /sound/eq/
    renders the editor, so no other page pays for it."""
    import jasper.camilla
    import jasper.sound.graph_carrier as graph_carrier

    # A reachable controller with a real loaded config, so nothing but the page
    # mode can be what stops the probe.
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(_room_config())
    monkeypatch.setattr(
        jasper.camilla, "primary_controller", lambda: FakeCamilla(str(current)),
    )

    def _must_not_probe(*_args, **_kwargs):
        raise AssertionError("a hardware page must not probe the loaded graph")

    monkeypatch.setattr(graph_carrier, "eq_block_for_loaded_config", _must_not_probe)

    with sound_server(tmp_path) as base:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/state", headers={"X-JTS-Sound-Page": header},
            )
        ) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))

    assert payload["eq_carrier"] == {"status": "unknown"}


async def test_apply_profile_preserves_active_room_peqs(tmp_path: Path, monkeypatch):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"

    payload = await sound_setup._apply_profile(
        SoundProfile(curve_id="bk", simple_eq=SimpleEq(treble_db=1.5)),
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.loaded_path is not None
    generated = Path(fake.loaded_path).read_text()
    assert Path(fake.loaded_path).name == "sound_current.yml"
    assert "room_peq_1:" in generated
    assert "sound_curve_bass:" in generated
    assert payload["preserved_room_peqs"] == 1
    assert payload["dsp_write_epoch"] == payload["last_dsp_apply"]["op_id"]
    assert load_profile(profile_path).curve_id == "bk"


async def test_reconcile_current_dsp_reemits_saved_profile_without_restamping(
    tmp_path: Path, monkeypatch,
):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    save_profile(
        SoundProfile(
            simple_eq=SimpleEq(bass_db=6.0),
            updated_at="2020-01-01T00:00:00+00:00",
        ),
        profile_path,
    )

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert payload["status"] == "reconciled"
    assert Path(fake.loaded_path).name == "sound_current.yml"
    generated = Path(fake.loaded_path).read_text()
    assert "room_peq_1:" in generated
    assert "sound_simple_bass:" in generated
    assert "id=reconcile-current-dsp" in generated
    assert load_profile(profile_path).updated_at == "2020-01-01T00:00:00+00:00"


async def test_reconcile_current_dsp_skips_unknown_config(
    tmp_path: Path, monkeypatch, caplog,
):
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = tmp_path / "custom.yml"
    current.write_text("# handmade\n")
    fake = FakeCamilla(str(current))
    caplog.set_level(logging.INFO, logger="jasper.sound.runtime")

    payload = await reconcile_current_dsp(
        profile_path=tmp_path / "sound_profile.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert payload["status"] == "skipped"
    assert payload["reason"] == "unknown_config"
    assert fake.loaded_path is None
    assert not (tmp_path / "dsp.json").exists()
    _, fields = _event_record(caplog, "sound.reconcile_current_dsp")
    assert fields["result"] == "skipped"
    assert fields["reason"] == "unknown_config"


async def test_reconcile_current_dsp_skips_active_audition_without_promoting(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    audition = config_dir / "sound_audition.yml"
    audition.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(audition))
    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=6.0)), profile_path)

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert payload["status"] == "skipped"
    assert payload["reason"] == "active_audition"
    assert fake.loaded_path is None
    # The audition file is untouched by the skip: its preference frame is
    # present either way, so "not promoted" is proved by nothing being loaded
    # and the file's bytes not moving.
    assert "sound_simple_bass:" in audition.read_text()
    assert not (config_dir / "sound_current.yml").exists()
    assert not (tmp_path / "dsp.json").exists()


async def test_reconcile_current_dsp_logs_unchanged_config(
    tmp_path: Path, monkeypatch, caplog,
):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    # Realistic apply-then-redeploy: the wizard save stamps sound_current.yml
    # with a wall-clock ``time.time_ns()`` id, NOT the reconcile id, and a
    # redeploy's dry-run re-emits the SAME profile under RECONCILE_PROFILE_ID —
    # so the two files differ ONLY in the cosmetic ``(id=...)`` header, which
    # must still read as unchanged. The timestamp id below is load-bearing: a
    # pre-stamped reconcile id would make the raw byte comparison match by
    # accident, and the production no-op path never sees that id.
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    profile = SoundProfile(simple_eq=SimpleEq(bass_db=2.0))
    current = config_dir / "sound_current.yml"
    current.write_text(_room_config())
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    save_profile(profile, profile_path)
    await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
        force=True,
    )
    current.write_text(
        re.sub(
            r"id=reconcile-current-dsp",
            "id=1717000000000000001",
            current.read_text(),
            count=1,
        )
    )
    fake.loaded_path = None
    assert "id=1717000000000000001" in current.read_text()
    assert "id=reconcile-current-dsp" not in current.read_text()
    caplog.set_level(logging.INFO, logger="jasper.sound.runtime")

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert payload["status"] == "unchanged"
    assert fake.loaded_path is None
    # The on-disk file is left untouched — its original (timestamp) id survives,
    # proving reconcile took the no-op path rather than re-emitting.
    assert "id=1717000000000000001" in current.read_text()
    assert _event_record(caplog, "sound.reconcile_current_dsp")[1]["result"] == (
        "unchanged"
    )


def test_config_id_header_strip_only_touches_the_header_line():
    # The unchanged-detection normalizer must collapse ONLY the cosmetic
    # ``# Auto-generated JTS DSP config (id=...).`` header line. A ``(id=...)``
    # span anywhere else in the YAML (e.g. inside a device name) must survive,
    # so a genuine change to such a value still compares as different and can
    # never be masked as "unchanged". Guards against an unanchored global sub.
    header_ts = (
        "---\n"
        "# Auto-generated JTS DSP config (id=1717000000000000001).\n"
        'devices:\n  device: "hw:CARD=x (id=realA)"\n'
    )
    header_reconcile = (
        "---\n"
        "# Auto-generated JTS DSP config (id=reconcile-current-dsp).\n"
        'devices:\n  device: "hw:CARD=x (id=realA)"\n'
    )
    # Differing header ids collapse to the same normalized text...
    assert _config_without_id_header(header_ts) == _config_without_id_header(
        header_reconcile
    )
    # ...and the header line loses only its marker.
    assert "# Auto-generated JTS DSP config.\n" in _config_without_id_header(header_ts)

    # A ``(id=...)`` substring OUTSIDE the header is preserved verbatim.
    assert '(id=realA)' in _config_without_id_header(header_ts)

    # Same header id, but a different device-name ``(id=...)`` span: the configs
    # must stay DIFFERENT after normalization (no masking of a real change).
    device_a = header_reconcile
    device_b = header_reconcile.replace("(id=realA)", "(id=realB)")
    assert _config_without_id_header(device_a) != _config_without_id_header(device_b)


@pytest.mark.parametrize("match_loudness", [False, True])
async def test_apply_profile_trims_the_output_only_when_match_loudness_is_on(
    tmp_path: Path, monkeypatch, match_loudness,
):
    """By default boosts boost. The preamp filter is DEFINED either way — its
    presence must never depend on a value — and inert at 0 dB until
    match-loudness asks for a trim."""
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    settings_path.write_text(json.dumps({"match_loudness": match_loudness}))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(_room_config())
    fake = FakeCamilla(str(current))

    payload = await sound_setup._apply_profile(
        SoundProfile(simple_eq=SimpleEq(bass_db=6.0)),
        profile_path=tmp_path / "sound_profile.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    generated = Path(fake.loaded_path).read_text()
    assert "sound_simple_bass:" in generated
    assert "  sound_preamp:" in generated
    assert (payload["output_trim_db"] > 0) is match_loudness
    assert (_preamp_gain(generated) == 0.0) is not match_loudness


async def test_apply_settings_reapplies_with_trim_without_restamping_profile(
    tmp_path: Path, monkeypatch
):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(_room_config())
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    # An applied profile with a boost, stamped at a fixed time.
    save_profile(
        SoundProfile(
            simple_eq=SimpleEq(bass_db=6.0), updated_at="2020-01-01T00:00:00+00:00"
        ),
        profile_path,
    )

    payload = await sound_setup._apply_settings(
        SoundSettings(match_loudness=True, volume_floor_db=-24.0),
        profile_path=profile_path,
        library_path=tmp_path / "lib.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    generated = Path(fake.loaded_path).read_text()
    assert "sound_preamp:" in generated  # match-loudness trim applied
    assert payload["output_trim_db"] > 0
    assert "warning" not in payload
    assert load_sound_settings(settings_path).match_loudness is True
    assert load_sound_settings(settings_path).volume_floor_db == -24.0
    # The profile JSON is untouched: not re-stamped, not overwritten.
    assert load_profile(profile_path).updated_at == "2020-01-01T00:00:00+00:00"


async def test_apply_settings_merges_only_recognized_posted_fields(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    settings_path.write_text(
        json.dumps({
            "headroom_trim_db": 4.0,
            "match_loudness": False,
            "volume_floor_db": -36.0,
        })
    )
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(_room_config())
    fake = FakeCamilla(str(current))

    payload = await sound_setup._apply_settings(
        {"match_loudness": True, "not_a_sound_setting": "discard me"},
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "lib.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    merged = load_sound_settings(settings_path)
    assert merged == SoundSettings(
        headroom_trim_db=4.0,
        match_loudness=True,
        volume_floor_db=-36.0,
    )
    assert payload["sound_settings"] == merged.to_dict()
    assert "not_a_sound_setting" not in json.loads(settings_path.read_text())


def test_concurrent_split_page_settings_merge_and_live_state_converge(
    tmp_path: Path, monkeypatch,
) -> None:
    settings_path = tmp_path / "sound_settings.json"
    profile_path = tmp_path / "sound_profile.json"
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    profile = SoundProfile(simple_eq=SimpleEq(bass_db=6.0))
    save_profile(profile, profile_path)

    start = threading.Barrier(3)
    attempted = [threading.Event(), threading.Event()]
    reemit_entered = threading.Event()
    allow_reemit = threading.Event()
    reconcile_entered = threading.Event()
    allow_reconcile = threading.Event()
    call_lock = threading.Lock()
    save_snapshots = []
    emitted = []
    reconciled_floors = []
    worker_context = threading.local()

    real_save = sound_profile_apply.save_sound_settings

    def recording_save(settings):
        real_save(settings)
        with call_lock:
            save_snapshots.append(settings.to_dict())

    async def fake_reemit(_profile, **kwargs):
        with call_lock:
            call_index = len(emitted)
            emitted.append({
                "worker": worker_context.index,
                "trim": kwargs["output_trim_db"],
                "settings": load_sound_settings().to_dict(),
            })
        if call_index == 0:
            reemit_entered.set()
            assert allow_reemit.wait(timeout=2.0)
        state = DspApplyState(
            schema_version=1,
            op_id=f"settings-{call_index}",
            source="sound_settings",
            phase="done",
            result="success",
            started_at="2026-08-04T00:00:00Z",
            finished_at="2026-08-04T00:00:01Z",
            prior_config_path=None,
            candidate_config_path=str(config_dir / f"settings-{call_index}.yml"),
            room_peq_count=0,
        )
        return state, config_dir / f"settings-{call_index}.yml", _profile

    async def fake_reconcile(**_kwargs):
        with call_lock:
            call_index = len(reconciled_floors)
            reconciled_floors.append(load_sound_settings().volume_floor_db)
        if call_index == 0:
            reconcile_entered.set()
            assert allow_reconcile.wait(timeout=2.0)
        return True

    monkeypatch.setattr(sound_profile_apply, "save_sound_settings", recording_save)
    monkeypatch.setattr(sound_profile_apply, "_load_profile_config", fake_reemit)
    monkeypatch.setattr(
        sound_profile_apply,
        "_reconcile_volume_curve_after_settings",
        fake_reconcile,
    )

    patches = (
        {"match_loudness": True},
        {"headroom_trim_db": 6.0, "volume_floor_db": -30.0},
    )

    def worker(index: int):
        start.wait(timeout=2.0)
        attempted[index].set()
        worker_context.index = index
        response = asyncio.run(
            sound_setup._apply_settings(
                patches[index],
                profile_path=profile_path,
                config_dir=config_dir,
                camilla_factory=lambda: None,
            )
        )
        return index, response

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, index) for index in range(2)]
        start.wait(timeout=2.0)
        assert attempted[0].wait(timeout=1.0)
        assert attempted[1].wait(timeout=1.0)
        assert reemit_entered.wait(timeout=1.0)
        assert len(save_snapshots) == 1
        allow_reemit.set()
        assert reconcile_entered.wait(timeout=1.0)
        assert len(save_snapshots) == 1
        allow_reconcile.set()
        responses = dict(future.result(timeout=3.0) for future in futures)

    merged = load_sound_settings()
    assert merged == SoundSettings(
        headroom_trim_db=6.0,
        match_loudness=True,
        volume_floor_db=-30.0,
    )
    assert len(save_snapshots) == 2
    assert emitted[-1]["settings"] == merged.to_dict()
    assert emitted[-1]["trim"] == sound_profile_apply._output_trim(profile, merged)
    assert reconciled_floors[-1] == -30.0
    assert responses[emitted[-1]["worker"]]["sound_settings"] == merged.to_dict()


@pytest.mark.parametrize("first_operation", ("profile", "settings"))
def test_concurrent_profile_and_settings_apply_converge_in_both_orders(
    first_operation: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Profile and Settings Apply linearize one complete Sound transaction.

    Force each operation to own the boundary first while the other attempts to
    enter. Whichever runs second must fresh-read the first operation's durable
    intent and leave disk plus the live graph/trim at the same P1/S1 pair.
    """

    settings_path = tmp_path / "sound_settings.json"
    profile_path = tmp_path / "sound_profile.json"
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))

    initial_profile = SoundProfile(
        curve_id="flat",
        simple_eq=SimpleEq(bass_db=1.0),
        updated_at="2026-08-01T00:00:00+00:00",
    )
    requested_profile = SoundProfile(
        curve_id="bk",
        simple_eq=SimpleEq(bass_db=6.0, treble_db=1.5),
    )
    initial_settings = SoundSettings(
        headroom_trim_db=1.0,
        match_loudness=False,
        volume_floor_db=-48.0,
    )
    requested_settings = SoundSettings(
        headroom_trim_db=5.0,
        match_loudness=True,
        volume_floor_db=-30.0,
    )
    save_profile(initial_profile, profile_path)
    sound_profile_apply.save_sound_settings(initial_settings)

    first_inside_emit = threading.Barrier(2)
    release_first_emit = threading.Event()
    second_boundary_attempted = threading.Event()
    second_entered_emit = threading.Event()
    record_lock = threading.Lock()
    entered_sources = []
    live_emits = []
    reconciled_floors = []
    settings_reads = []
    worker_role = threading.local()

    class ObservedSoundStateLock:
        """Expose the second worker's exact attempt at the real boundary."""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._owner: int | None = None

        def __enter__(self):
            if getattr(worker_role, "value", None) == "second":
                second_boundary_attempted.set()
            self._lock.acquire()
            self._owner = threading.get_ident()
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> None:
            self._owner = None
            self._lock.release()

        def owned_by_current_thread(self) -> bool:
            return self._owner == threading.get_ident()

    observed_state_lock = ObservedSoundStateLock()
    monkeypatch.setattr(
        sound_profile_apply,
        "_sound_state_write_lock",
        observed_state_lock,
    )
    real_load_sound_settings = sound_profile_apply.load_sound_settings

    def observed_load_sound_settings(*args, **kwargs):
        if getattr(worker_role, "value", None) in {"first", "second"}:
            with record_lock:
                settings_reads.append(observed_state_lock.owned_by_current_thread())
        return real_load_sound_settings(*args, **kwargs)

    monkeypatch.setattr(
        sound_profile_apply,
        "load_sound_settings",
        observed_load_sound_settings,
    )

    async def fake_emit(profile, **kwargs):
        source = kwargs["source"]
        with record_lock:
            call_index = len(entered_sources)
            entered_sources.append(source)
        if call_index == 0:
            first_inside_emit.wait(timeout=2.0)
            assert release_first_emit.wait(timeout=2.0)
        else:
            second_entered_emit.set()

        state = DspApplyState(
            schema_version=1,
            op_id=f"{source}-{call_index}",
            source=source,
            phase="done",
            result="success",
            started_at="2026-08-04T00:00:00Z",
            finished_at="2026-08-04T00:00:01Z",
            prior_config_path=None,
            candidate_config_path=str(config_dir / f"{source}-{call_index}.yml"),
            room_peq_count=0,
        )
        if kwargs["persist_profile"]:
            save_profile(profile, profile_path)
        with record_lock:
            live_emits.append({
                "source": source,
                "profile": profile.to_dict(),
                "settings": load_sound_settings().to_dict(),
                "trim": kwargs["output_trim_db"],
                "epoch": state.op_id,
            })
        return state, Path(state.candidate_config_path), profile

    async def fake_reconcile(**_kwargs):
        reconciled_floors.append(load_sound_settings().volume_floor_db)
        return True

    monkeypatch.setattr(sound_profile_apply, "_load_profile_config", fake_emit)
    monkeypatch.setattr(
        sound_profile_apply,
        "_reconcile_volume_curve_after_settings",
        fake_reconcile,
    )

    def run(operation: str, role: str):
        worker_role.value = role
        if operation == "profile":
            return asyncio.run(
                sound_setup._apply_profile(
                    requested_profile,
                    profile_path=profile_path,
                    config_dir=config_dir,
                    camilla_factory=lambda: None,
                )
            )
        return asyncio.run(
            sound_setup._apply_settings(
                requested_settings.to_dict(),
                profile_path=profile_path,
                config_dir=config_dir,
                camilla_factory=lambda: None,
            )
        )

    second_operation = "settings" if first_operation == "profile" else "profile"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(run, first_operation, "first")
        first_inside_emit.wait(timeout=2.0)

        second_future = executor.submit(run, second_operation, "second")
        assert second_boundary_attempted.wait(timeout=1.0)
        # Before the first transaction is released, the second must be waiting at
        # the shared ordering boundary rather than entering the DSP transaction.
        assert not second_entered_emit.wait(timeout=0.25)
        assert entered_sources == [
            "sound" if first_operation == "profile" else "sound_settings"
        ]
        release_first_emit.set()

        responses = {
            first_operation: first_future.result(timeout=3.0),
            second_operation: second_future.result(timeout=3.0),
        }

    final_profile = load_profile(profile_path)
    final_settings = load_sound_settings()
    assert final_profile.curve_id == requested_profile.curve_id
    assert final_profile.simple_eq == requested_profile.simple_eq
    assert final_settings == requested_settings
    assert len(live_emits) == 2
    assert live_emits[-1]["profile"] == final_profile.to_dict()
    assert live_emits[-1]["settings"] == final_settings.to_dict()
    assert live_emits[-1]["trim"] == sound_profile_apply._output_trim(
        final_profile,
        final_settings,
    )
    assert reconciled_floors == [requested_settings.volume_floor_db]
    assert settings_reads
    assert all(settings_reads)

    emits_by_operation = {
        "profile" if emit["source"] == "sound" else "settings": emit
        for emit in live_emits
    }
    for operation, response in responses.items():
        emit = emits_by_operation[operation]
        assert response["profile"] == emit["profile"]
        assert response["sound_settings"] == emit["settings"]
        assert response["output_trim_db"] == emit["trim"]
        assert response["dsp_write_epoch"] == emit["epoch"]
        assert response["last_dsp_apply"]["op_id"] == emit["epoch"]

    last_response = responses[second_operation]
    assert last_response["profile"] == final_profile.to_dict()
    assert last_response["sound_settings"] == final_settings.to_dict()
    assert last_response["dsp_write_epoch"] == live_emits[-1]["epoch"]
    assert last_response["last_dsp_apply"]["op_id"] == live_emits[-1]["epoch"]


async def test_audition_volume_floor_holds_updates_and_restores_on_stop(
    tmp_path: Path, monkeypatch,
):
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("JASPER_VOLUME_FLOOR_TONE_DIR", str(tmp_path / "tones"))
    FakeVolumeFloorToneRunner.instances.clear()
    fake = FakeVolumeCamilla(db=-18.0, muted=True)
    _install_floor_tone_owner(fake)
    session = volume_floor_tone.VolumeFloorToneSession()

    payload = await session.start_or_update(
        {"volume_floor_db": -24.0},
        camilla_factory=lambda: fake,
        runner_factory=FakeVolumeFloorToneRunner,
    )

    assert payload == {
        "ok": True,
        "active": True,
        "continuous": True,
        "status": "started",
        "volume_floor_db": -24.0,
        "percent": 1,
        "db": round(percent_to_db(1, floor_db=-24.0), 3),
    }
    assert len(FakeVolumeFloorToneRunner.instances) == 1
    assert FakeVolumeFloorToneRunner.instances[0].started is True
    assert fake.events[0] == (
        "volume", pytest.approx(percent_to_db(1, floor_db=-24.0)), True,
    )
    assert fake.events[1] == ("mute", False, False)
    assert fake.db == pytest.approx(percent_to_db(1, floor_db=-24.0))
    assert fake.muted is False
    assert not settings_path.exists()

    payload = await session.start_or_update(
        {"volume_floor_db": -36.0},
        camilla_factory=lambda: fake,
        runner_factory=FakeVolumeFloorToneRunner,
    )

    assert payload["status"] == "updated"
    assert payload["volume_floor_db"] == -36.0
    assert len(FakeVolumeFloorToneRunner.instances) == 1
    assert fake.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(1, floor_db=-36.0)), True),
        ("mute", False, False),
    ]
    assert fake.db == pytest.approx(percent_to_db(1, floor_db=-36.0))
    assert fake.muted is False

    stop_payload = await session.stop(
        camilla_factory=lambda: fake,
        reason="stop",
    )

    assert stop_payload == {
        "ok": True,
        "active": False,
        "status": "stopped",
        "reason": "stop",
        "volume_floor_db": -36.0,
    }
    assert FakeVolumeFloorToneRunner.instances[0].stopped is True
    assert fake.events[-2:] == [
        ("mute", True, True),
        ("volume", pytest.approx(-18.0), True),
    ]
    assert fake.db == pytest.approx(-18.0)
    assert fake.muted is True
    assert not settings_path.exists()


async def test_audition_volume_floor_update_survives_a_withdrawn_owner(
    tmp_path: Path, monkeypatch,
):
    """An owner withdrawn mid-audition degrades the update, it does not fail it.

    Same contract as starting with no owner registered: the tone keeps
    playing at the floor it already holds rather than the request raising.
    """
    from jasper.volume_owner import install_volume_owner

    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("JASPER_VOLUME_FLOOR_TONE_DIR", str(tmp_path / "tones"))
    FakeVolumeFloorToneRunner.instances.clear()
    fake = FakeVolumeCamilla(db=-18.0, muted=False)
    _install_floor_tone_owner(fake)
    session = volume_floor_tone.VolumeFloorToneSession()

    await session.start_or_update(
        {"volume_floor_db": -24.0},
        camilla_factory=lambda: fake,
        runner_factory=FakeVolumeFloorToneRunner,
    )
    held_db = fake.db
    install_volume_owner(None)

    payload = await session.start_or_update(
        {"volume_floor_db": -36.0},
        camilla_factory=lambda: fake,
        runner_factory=FakeVolumeFloorToneRunner,
    )

    assert payload["status"] == "updated"
    assert payload["volume_floor_db"] == -36.0
    assert fake.db == pytest.approx(held_db)
    assert FakeVolumeFloorToneRunner.instances[0].stopped is False


def _dominant_frequency_hz(samples: np.ndarray, sample_rate: int) -> float:
    window = np.hanning(len(samples))
    spectrum = np.fft.rfft(samples.astype(np.float64) * window)
    bins = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)
    peak = int(np.argmax(np.abs(spectrum)))
    return float(bins[peak])


async def test_camilla_op_lock_is_released_when_a_waiter_is_cancelled():
    """A cancelled waiter must not strand the CamillaDSP op lock: every later
    audition and stop would block forever with the fader down at the floor."""
    session = volume_floor_tone.VolumeFloorToneSession()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with session._camilla_op():
            entered.set()
            await release.wait()

    async def take() -> None:
        async with session._camilla_op():
            pass

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    waiter = asyncio.create_task(take())
    await asyncio.sleep(0.1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder

    await asyncio.wait_for(take(), timeout=2.0)


def test_volume_floor_reference_tone_uses_low_mid_high_sequence(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("JASPER_VOLUME_FLOOR_TONE_DIR", str(tmp_path / "tones"))

    wav_path = volume_floor_tone._volume_floor_tone_wav_path()

    assert wav_path.name.startswith("volume_floor_reference_")
    with wave.open(str(wav_path), "rb") as wav:
        sample_rate = wav.getframerate()
        assert wav.getnchannels() == 1
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)

    segment_n = int(
        round(volume_floor_tone.VOLUME_FLOOR_TONE_SEGMENT_DURATION_S * sample_rate)
    )
    for index, expected in enumerate(volume_floor_tone.VOLUME_FLOOR_TONE_FREQS_HZ):
        segment = pcm[index * segment_n:(index + 1) * segment_n]
        assert _dominant_frequency_hz(segment, sample_rate) == pytest.approx(
            expected,
            abs=3.0,
        )


async def test_volume_floor_stop_stops_runner_before_slow_update_restore(
    tmp_path: Path, monkeypatch,
):
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("JASPER_VOLUME_FLOOR_TONE_DIR", str(tmp_path / "tones"))
    FakeVolumeFloorToneRunner.instances.clear()
    fake = BlockingVolumeCamilla(block_on_volume_call=2)
    _install_floor_tone_owner(fake)
    session = volume_floor_tone.VolumeFloorToneSession()

    await session.start_or_update(
        {"volume_floor_db": -24.0},
        camilla_factory=lambda: fake,
        runner_factory=FakeVolumeFloorToneRunner,
    )
    runner = FakeVolumeFloorToneRunner.instances[0]

    update_task = asyncio.create_task(
        session.start_or_update(
            {"volume_floor_db": -36.0},
            camilla_factory=lambda: fake,
            runner_factory=FakeVolumeFloorToneRunner,
        )
    )
    await asyncio.wait_for(fake.volume_call_entered.wait(), timeout=1.0)

    stop_task = asyncio.create_task(
        session.stop(
            camilla_factory=lambda: fake,
            reason="stop",
        )
    )
    await asyncio.sleep(0)

    assert runner.stopped is True
    assert stop_task.done() is False

    fake.release_volume_call.set()
    update_payload = await asyncio.wait_for(update_task, timeout=1.0)
    stop_payload = await asyncio.wait_for(stop_task, timeout=1.0)

    assert update_payload["active"] is False
    assert update_payload["status"] == "stale"
    assert stop_payload["status"] == "stopped"
    assert fake.events[-2:] == [
        ("mute", True, True),
        ("volume", pytest.approx(-18.0), True),
    ]
    assert fake.db == pytest.approx(-18.0)
    assert fake.muted is True


async def test_apply_settings_warns_but_keeps_settings_on_reapply_failure(
    tmp_path: Path, monkeypatch
):
    # Without a saved layout the re-apply refuses before it ever reaches the
    # failing reload, and a refusal is a typed body, not this warning.
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current), fail_set=True)  # reload fails

    payload = await sound_setup._apply_settings(
        SoundSettings(headroom_trim_db=6.0),
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "lib.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert "warning" in payload
    # Settings persist despite the re-apply failure (no revert, no silent loss).
    assert load_sound_settings(settings_path).headroom_trim_db == 6.0


async def test_apply_settings_returns_the_typed_refusal_not_a_prose_warning(
    tmp_path: Path, monkeypatch
):
    """A graph that cannot host EQ is a typed outcome on /settings too.

    The setting is saved; only the re-emit is refused. The body carries the
    same status/reason_code the /apply and /live-draft refusals return, so the
    settings card branches on one shape instead of parsing a warning string.
    """
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = tmp_path / "custom.yml"
    current.write_text("# handmade\n")
    fake = FakeCamilla(str(current))

    payload = await sound_setup._apply_settings(
        {"match_loudness": True},
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "lib.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "unknown_config"
    assert "warning" not in payload
    assert load_sound_settings(settings_path).match_loudness is True


async def test_audition_profile_loads_draft_without_persisting(
    tmp_path: Path,
    monkeypatch,
):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    # match-loudness on -> the audition gets a loudness-weighted output trim.
    settings_path = tmp_path / "sound_settings.json"
    settings_path.write_text('{"match_loudness": true}')
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    draft = SoundProfile(
        curve_id="harman",
        parametric_bands=(ParametricBand(freq_hz=1000.0, gain_db=3.0, q=1.0),),
    )

    payload = await sound_setup._audition_profile(
        draft,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.loaded_path is not None
    assert Path(fake.loaded_path).name == "sound_audition.yml"
    generated = Path(fake.loaded_path).read_text()
    assert "sound_curve_bass:" in generated
    assert "sound_advanced_1:" in generated
    assert "sound_preamp:" in generated  # match-loudness trim applied
    assert payload["audition_profile"]["curve_id"] == "harman"
    assert payload["output_trim_db"] > 0
    assert payload["dsp_write_epoch"] == payload["last_dsp_apply"]["op_id"]
    assert not profile_path.exists()


async def test_live_draft_profile_updates_active_config_without_persisting(
    tmp_path: Path,
    monkeypatch,
):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    state_path = tmp_path / "dsp_apply_state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state_path))
    _record_dsp_epoch(state_path, "epoch-1")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    draft = SoundProfile(curve_id="harman", simple_eq=SimpleEq(bass_db=2.0))

    payload = await sound_setup._live_draft_profile(
        draft,
        expected_dsp_write_epoch=dsp_write_epoch(),
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.set_calls == []
    assert len(fake.active_raw_values) == 1
    assert "sound_curve_bass:" in fake.active_raw_values[0]
    assert "room_peq_1:" in fake.active_raw_values[0]
    # Default settings -> no output trim, so boosts boost. The preamp is
    # always DEFINED (its presence must not depend on a value) and inert.
    assert "  sound_preamp:" in fake.active_raw_values[0]
    assert "gain: 0.0000" in fake.active_raw_values[0]
    assert payload["live_status"] == "live"
    assert fake.ducks[-1] is True
    assert payload["dsp_write_epoch"] == "epoch-1"
    assert not profile_path.exists()


async def _live(fake, draft, config_dir):
    return await sound_setup._live_draft_profile(
        draft,
        expected_dsp_write_epoch=dsp_write_epoch(),
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )


def _eq_box(monkeypatch, tmp_path):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    state_path = tmp_path / "dsp_apply_state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state_path))
    _record_dsp_epoch(state_path, "epoch-1")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    return config_dir, FakeCamilla(str(current))


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(
            SoundProfile(parametric_bands=(
                ParametricBand(freq_hz=1000.0, gain_db=5.0, q=1.0),
            )),
            id="gain",
        ),
        pytest.param(
            SoundProfile(parametric_bands=(
                ParametricBand(freq_hz=120.0, gain_db=2.0, q=1.0),
            )),
            id="frequency",
        ),
        pytest.param(
            SoundProfile(parametric_bands=(
                ParametricBand(freq_hz=1000.0, gain_db=2.0, q=4.5),
            )),
            id="q",
        ),
    ],
)
async def test_dragging_a_band_writes_parameters_and_never_swaps_the_pipeline(
    tmp_path: Path, monkeypatch, moved,
):
    """The whole point: a drag must not reach the ducked swap path."""
    config_dir, fake = _eq_box(monkeypatch, tmp_path)
    start = SoundProfile(parametric_bands=(
        ParametricBand(freq_hz=1000.0, gain_db=2.0, q=1.0),
    ))
    await _live(fake, start, config_dir)
    swaps_after_install = len(fake.active_raw_values)
    assert fake.ducks[0] is True

    payload = await _live(fake, moved, config_dir)

    assert payload["live_status"] == "live"
    assert fake.ducks[-1] is False
    assert len(fake.active_raw_values) == swaps_after_install + 1


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(
            SoundProfile(parametric_bands=(
                ParametricBand(freq_hz=1000.0, gain_db=2.0, q=1.0),
                ParametricBand(freq_hz=60.0, gain_db=3.0, q=1.0),
            )),
            id="band_added",
        ),
        pytest.param(
            SoundProfile(parametric_bands=()),
            id="band_removed",
        ),
        pytest.param(
            SoundProfile(parametric_bands=(
                ParametricBand(freq_hz=1000.0, gain_db=0.0, q=1.0),
            )),
            id="gain_dragged_to_exactly_flat",
        ),
        pytest.param(
            SoundProfile(curve_id="harman", parametric_bands=(
                ParametricBand(freq_hz=1000.0, gain_db=2.0, q=1.0),
            )),
            id="curve_preset_changed",
        ),
    ],
)
async def test_the_live_graphs_slots_keep_the_pipeline_still(
    tmp_path: Path, monkeypatch, changed,
):
    """Adding, removing or flattening a band, or switching the curve preset,
    writes numbers, not a pipeline.

    The live draft carries a slot per band and one fixed pair of curve shelves,
    so none of these changes which filters exist — which is what would
    otherwise rebuild CamillaDSP's filter group and reset every filter's state.
    """
    config_dir, fake = _eq_box(monkeypatch, tmp_path)
    one = SoundProfile(parametric_bands=(
        ParametricBand(freq_hz=1000.0, gain_db=2.0, q=1.0),
    ))
    await _live(fake, one, config_dir)
    swaps_after_install = len(fake.active_raw_values)

    payload = await _live(fake, changed, config_dir)

    assert payload["live_status"] == "live"
    assert fake.ducks[-1] is False
    assert len(fake.active_raw_values) == swaps_after_install + 1


async def test_the_live_trim_is_frozen_so_an_edit_cannot_step_the_level(
    tmp_path: Path, monkeypatch,
):
    """Match-loudness must not move the trim mid-edit.

    The trim is a function of the profile's own EQ when match-loudness is on.
    Derived from the DRAFT it would fold into ``active_baseline_headroom``'s
    value and be written in place — an instant, un-ducked, full-spectrum
    level step. Derived from the SAVED profile it is one number for the whole
    session.
    """
    config_dir, fake = _eq_box(monkeypatch, tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"match_loudness": true}')
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    # Saved intent is flat, so the frozen trim is whatever flat earns...
    profile_path = tmp_path / "sound_profile.json"
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile_path))
    save_profile(SoundProfile(), profile_path)

    quiet = SoundProfile(parametric_bands=(
        ParametricBand(freq_hz=1000.0, gain_db=1.0, q=1.0),
    ))
    # ...and a draft loud enough that a LIVE trim would visibly differ.
    loud = SoundProfile(parametric_bands=(
        ParametricBand(freq_hz=100.0, gain_db=12.0, q=0.5),
        ParametricBand(freq_hz=1000.0, gain_db=12.0, q=0.5),
        ParametricBand(freq_hz=8000.0, gain_db=12.0, q=0.5),
    ))
    assert loudness_compensation_db(loud) > loudness_compensation_db(quiet)

    await _live(fake, quiet, config_dir)
    after_quiet = fake.running
    second = await _live(fake, loud, config_dir)
    after_loud = fake.running

    # The preamp is always present; what must not move is its GAIN. A
    # draft-derived trim would have turned the loud draft down.
    assert _preamp_gain(after_quiet) == _preamp_gain(after_loud)
    # Same filter set both times, so the edit is a parameter write and the
    # broadband gain never steps.
    import yaml as _yaml

    assert set(_yaml.safe_load(after_quiet)["filters"]) == set(
        _yaml.safe_load(after_loud)["filters"]
    )
    assert second["live_status"] == "live"
    assert fake.ducks[-1] is False
    assert len(fake.active_raw_values) == 2


async def test_retyping_a_band_writes_parameters_without_ducking(
    tmp_path: Path, monkeypatch,
):
    """A biquad's type lives in its parameters; CamillaDSP recomputes coefficients in place."""
    config_dir, fake = _eq_box(monkeypatch, tmp_path)
    one = SoundProfile(parametric_bands=(
        ParametricBand(freq_hz=1000.0, gain_db=2.0, q=1.0),
    ))
    await _live(fake, one, config_dir)
    swaps_after_install = len(fake.active_raw_values)
    retyped = SoundProfile(parametric_bands=(
        ParametricBand(biquad_type="Highshelf", freq_hz=1000.0, gain_db=2.0, q=1.0),
    ))

    payload = await _live(fake, retyped, config_dir)

    assert payload["live_status"] == "live"
    assert fake.ducks[-1] is False
    assert len(fake.active_raw_values) == swaps_after_install + 1


async def test_a_redraw_that_changed_nothing_writes_nothing(
    tmp_path: Path, monkeypatch,
):
    config_dir, fake = _eq_box(monkeypatch, tmp_path)
    draft = SoundProfile(parametric_bands=(
        ParametricBand(freq_hz=1000.0, gain_db=2.0, q=1.0),
    ))
    await _live(fake, draft, config_dir)

    payload = await _live(fake, draft, config_dir)

    assert payload["live_status"] == "live"
    assert len(fake.active_raw_values) == 1


async def test_live_draft_profile_skips_stale_epoch_without_touching_audio(
    tmp_path: Path,
    monkeypatch,
):
    state_path = tmp_path / "dsp_apply_state.json"
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(state_path),
    )
    _record_dsp_epoch(state_path, "newer-apply")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(_room_config())
    fake = FakeCamilla(str(current))
    draft = SoundProfile(curve_id="bk", simple_eq=SimpleEq(treble_db=1.0))

    payload = await sound_setup._live_draft_profile(
        draft,
        expected_dsp_write_epoch="older-apply",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.active_raw_values == []
    assert fake.set_calls == []
    assert payload["live_status"] == "stale"
    assert payload["dsp_write_epoch"] == "newer-apply"


async def test_live_draft_profile_reports_unavailable_without_reload(
    tmp_path: Path,
    monkeypatch,
):
    state_path = tmp_path / "dsp_apply_state.json"
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state_path))
    _record_dsp_epoch(state_path, "epoch-1")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "sound_current.yml"
    current.write_text(_room_config())
    fake = FakeCamillaWithoutLiveRaw(str(current))
    draft = SoundProfile(curve_id="bk", simple_eq=SimpleEq(treble_db=1.0))

    payload = await sound_setup._live_draft_profile(
        draft,
        expected_dsp_write_epoch="epoch-1",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.loaded_path is None
    assert fake.set_calls == []
    assert payload["live_status"] == "unavailable"


async def test_apply_profile_rejects_unknown_active_config(tmp_path: Path):
    current = tmp_path / "custom.yml"
    current.write_text("# handmade\n")
    fake = FakeCamilla(str(current))

    with pytest.raises(RuntimeError) as excinfo:
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=tmp_path / "configs",
            camilla_factory=lambda: fake,
        )
    # The durable path wraps the carrier refusal as DspApplyError; the route's
    # discrimination unwraps it to a stable, typed reason (a 200 body, not 502).
    refusal = sound_setup._carrier_refusal(excinfo.value)
    assert refusal is not None
    assert refusal.reason_code == "unknown_config"


def _active_baseline_config() -> str:
    from tests.test_active_speaker_runtime_contract import _active_baseline_yaml

    return _active_baseline_yaml("mono", 2)


def _program_bake_config() -> str:
    from tests.test_active_speaker_runtime_contract import _program_bake_yaml

    return _program_bake_yaml()


def _program_bake_member_kwargs(monkeypatch) -> None:
    monkeypatch.setattr(
        "jasper.multiroom.member_config.member_camilla_kwargs",
        lambda: {"enable_rate_adjust": True, "playback_pipe_path": None},
    )


@pytest.mark.parametrize(
    ("basename", "yaml_factory", "reason_code", "prepare"),
    [
        pytest.param(
            "active_speaker_baseline.yml",
            _active_baseline_config,
            "active_baseline_compile_unavailable",
            None,
            id="active_speaker_baseline",
        ),
        pytest.param(
            "grouping_active_leader_bake.yml",
            _program_bake_config,
            "program_bake_pipe_unavailable",
            _program_bake_member_kwargs,
            id="grouping_program_bake",
        ),
    ],
)
async def test_apply_profile_blocks_a_carrier_that_cannot_host_eq(
    tmp_path: Path, monkeypatch, basename, yaml_factory, reason_code, prepare,
):
    """A graph preference EQ cannot ride refuses by NAME and changes nothing.

    An applied active-speaker baseline used to hit the misleading "custom
    config ... Reset" 502 that would have DESTROYED the active graph if
    followed. A solo baseline can host preference EQ by recomposing from its
    saved evidence; here that evidence is absent (a bare tmp config dir), so
    the apply refuses with a specific, honest reason, never re-emits a stereo
    config over the active graph, and — since a refusal is a handled "blocked"
    outcome, not a DSP failure — records NO dsp-apply state (the pre-check
    dry-runs the active carrier), so jasper-doctor's check_dsp_apply_state
    stays clean on an active speaker.
    """
    from jasper.dsp_apply import last_dsp_apply_state

    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / basename).write_text(yaml_factory(), encoding="utf-8")
    fake = FakeCamilla(str(config_dir / basename))
    if prepare is not None:
        prepare(monkeypatch)

    with pytest.raises(RuntimeError) as excinfo:
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=config_dir,
            camilla_factory=lambda: fake,
        )

    refusal = sound_setup._carrier_refusal(excinfo.value)
    assert refusal is not None
    assert refusal.reason_code == reason_code
    assert refusal.to_payload()["status"] == "blocked"
    # Fail closed: the active config was never overwritten / re-loaded, and the
    # refusal raised before the apply transaction, so no failure state landed.
    assert fake.loaded_path is None
    assert last_dsp_apply_state() is None


def test_carrier_refusal_unwraps_raw_and_wrapped():
    from jasper.sound.graph_carrier import CarrierCannotHostEq

    raw = CarrierCannotHostEq("unknown_config", "m")
    assert sound_setup._carrier_refusal(raw) is raw

    # The durable path's in-lock re-check wraps the refusal as DspApplyError
    # (...) in the rare concurrent-swap race; the unwrap must still find it.
    try:
        try:
            raise raw
        except CarrierCannotHostEq as cause:
            raise RuntimeError("DSP config preparation failed: m") from cause
    except RuntimeError as wrapped:
        assert sound_setup._carrier_refusal(wrapped) is raw

    assert sound_setup._carrier_refusal(ValueError("unrelated")) is None


def test_apply_route_returns_200_blocked_for_active_config(tmp_path, monkeypatch):
    # The headline user-facing contract, exercised through the real do_POST
    # handler (the carrier unit tests cannot reach it). A graph that
    # can't host EQ yields HTTP 200 {status:"blocked"} — the page's honest-hint
    # vocabulary — never a 502 toast or a silent no-op. A regression that
    # dropped the handler's `return` (falling through to the 502 branch) would
    # pass every other test but fail this one.
    import io

    from jasper.dsp_apply import last_dsp_apply_state

    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    # CSRF / host guard is covered by its own tests; bypass it to drive dispatch.
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    active = config_dir / "active_speaker_baseline.yml"
    active.write_text(_active_baseline_config())
    fake = FakeCamilla(str(active))

    Handler = sound_setup._make_handler(
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "sound_profiles.json",
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )
    body = json.dumps({"enabled": True}).encode()
    raw = (
        b"POST /apply HTTP/1.1\r\nHost: jts.local\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"\r\n"
        + body
    )
    rfile = io.BytesIO(raw)
    wfile = io.BytesIO()
    handler = Handler.__new__(Handler)
    handler.rfile = rfile
    handler.wfile = wfile
    handler.client_address = ("127.0.0.1", 0)
    handler.server = None
    handler.raw_requestline = rfile.readline()
    handler.parse_request()
    handler.protocol_version = "HTTP/1.1"
    handler.do_POST()
    resp = wfile.getvalue()

    status_line = resp.split(b"\r\n", 1)[0]
    assert b"200" in status_line, status_line
    assert b"502" not in status_line
    payload = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode())
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "active_baseline_compile_unavailable"
    # Fail closed: active config never swapped, no prepare_failed state.
    assert fake.loaded_path is None
    assert last_dsp_apply_state() is None


async def test_apply_profile_rechecks_carrier_under_lock_against_concurrent_swap(
    tmp_path: Path, monkeypatch
):
    # TOCTOU guard: the carrier is re-resolved UNDER the dsp-apply writer
    # lock, so if the loaded config is swapped to an active graph between the
    # pre-lock fast-check and lock acquisition (a concurrent active-startup load
    # shares that lock), the durable apply refuses in-lock and NEVER re-emits a
    # stereo config over the active crossover. Simulated by a fake that reports
    # a hostable config on the first read (pre-lock) and an active graph on
    # every read after (in-lock).
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "sound_current.yml").write_text(_room_config())  # hostable
    (config_dir / "active_speaker_baseline.yml").write_text(
        _active_baseline_config()
    )

    class _RacingCamilla:
        def __init__(self) -> None:
            self.calls = 0
            self.loaded_path: str | None = None

        async def get_config_file_path(self, *, best_effort: bool = True):
            self.calls += 1
            name = "sound_current.yml" if self.calls == 1 else "active_speaker_baseline.yml"
            return str(config_dir / name)

        async def set_config_file_path(self, path, *, best_effort: bool = False):
            self.loaded_path = path

    cam = _RacingCamilla()
    with pytest.raises(RuntimeError) as excinfo:
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=config_dir,
            camilla_factory=lambda: cam,
        )
    refusal = sound_setup._carrier_refusal(excinfo.value)
    assert refusal is not None
    assert refusal.reason_code == "active_baseline_compile_unavailable"
    # The in-lock re-check fired (pre-check saw the hostable config first).
    assert cam.calls >= 2
    # The stereo config was NEVER loaded over the active crossover.
    assert cam.loaded_path is None


async def test_apply_profile_rolls_back_when_reload_fails(
    tmp_path: Path,
    monkeypatch,
):
    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current), fail_set=True)

    with pytest.raises(RuntimeError, match="reload failed"):
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=config_dir,
            camilla_factory=lambda: fake,
        )

    assert fake.set_calls[-1] == str(current)
    assert not (tmp_path / "sound_profile.json").exists()


def test_profile_library_route_helpers_create_rename_delete(tmp_path: Path):
    library_path = tmp_path / "sound_profiles.json"

    created = sound_setup.save_named_profile(
        SoundProfile(curve_id="harman"),
        name="Library Test",
        path=library_path,
    )
    renamed = sound_setup.rename_named_profile(
        created.id,
        name="Library Renamed",
        path=library_path,
    )
    sound_setup.delete_named_profile(renamed.id, path=library_path)

    assert load_profile_library(library_path) == ()


# --- same-shape composite re-pin (#2814) -------------------------------------


def _ported_dual_apple_topology_raw() -> dict:
    """A commissioned dual-Apple save, each child pinned to a USB port."""

    hardware = _dual_apple_hardware()
    for child, card_id, port in zip(
        hardware["child_devices"],
        ("A", "A_1"),
        ("usb1/1-2", "usb1/1-1"),
    ):
        child["card_id"] = card_id
        child["usb_path"] = port
        child["controller"] = "xhci-hcd.0"
    # The lane labels a real save carries (``topology_hardware_from_state``),
    # so the offer's disclosure is asserted against production wording.
    hardware["outputs"] = [
        {"index": 0, "human_label": "Apple DAC A left", "terminal_label": "A-L"},
        {"index": 1, "human_label": "Apple DAC A right", "terminal_label": "A-R"},
        {"index": 2, "human_label": "Apple DAC B left", "terminal_label": "B-L"},
        {"index": 3, "human_label": "Apple DAC B right", "terminal_label": "B-R"},
    ]

    def group(group_id: str, kind: str, woofer: int, tweeter: int) -> dict:
        return {
            "id": group_id,
            "label": group_id.title(),
            "kind": kind,
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "driver_style": "sealed_cone",
                    "physical_output_index": woofer,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "physical_output_index": tweeter,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                },
            ],
        }

    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "hardware": hardware,
        "speaker_groups": [group("left", "left", 0, 1), group("right", "right", 2, 3)],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    }


def _write_repin_fixture(
    monkeypatch,
    tmp_path: Path,
    *,
    attached_serial_b: str,
) -> None:
    """Save the commissioned pair, then observe whichever units are attached."""

    topology_path = tmp_path / "output_topology.json"
    hardware_path = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(hardware_path))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE", str(tmp_path / "baseline.json")
    )
    save_output_topology(
        OutputTopology.from_mapping(_ported_dual_apple_topology_raw()),
        path=topology_path,
    )
    _observe_apple_cards(tmp_path, LEFT_APPLE_SERIAL, attached_serial_b)


def _stub_repin_runtime(monkeypatch) -> list[str]:
    """Stand in for the audio-parking choreography a re-pin shares with save."""

    def park_and_commit(_topology, commit, **_kwargs):
        return _RuntimeMutation(commit())

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )
    monkeypatch.setattr(
        "jasper.output_topology_runtime.trigger_reconcile",
        lambda **_kwargs: {"ok": True},
    )
    return _stub_audio_stops(monkeypatch)


def test_output_topology_payload_offers_a_repin_only_for_a_swapped_dongle(
    monkeypatch,
    tmp_path: Path,
):
    """The wizard renders the offer from the payload; the server decides."""

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b="NEW-DONGLE")
    offered = sound_setup._output_topology_payload()["hardware_repin"]

    assert offered["child_count"] == 2
    assert offered["replaced_child_count"] == 1
    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b=RIGHT_APPLE_SERIAL)
    assert sound_setup._output_topology_payload()["hardware_repin"] is None


def test_repin_endpoint_keeps_the_design_and_drops_drift_evidence(
    monkeypatch,
    tmp_path: Path,
):
    """The saved artifact is the contract, not the response body.

    Everything a swapped unit cannot invalidate survives on disk; the pair's
    drift evidence is dropped.
    """

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b="NEW-DONGLE")
    park_kwargs: dict = {}
    stops = _stub_repin_runtime(monkeypatch)

    def park_and_commit(_topology, commit, **kwargs):
        park_kwargs.update(kwargs)
        _observe_apple_cards(tmp_path, LEFT_APPLE_SERIAL, "LATEST-DONGLE")
        return _RuntimeMutation(commit())

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )

    payload = sound_setup._repin_output_topology_payload({})

    assert payload["repin"]["status"] == "repinned"
    assert stops == ["safe"]
    assert park_kwargs["stay_parked"] is True

    saved = load_output_topology()
    assert [child.serial for child in saved.hardware.child_devices] == [
        LEFT_APPLE_SERIAL,
        "LATEST-DONGLE",
    ]
    # The design itself is untouched — this is the whole point of the flow.
    before = OutputTopology.from_mapping(_ported_dual_apple_topology_raw())
    assert saved.routing == before.routing
    assert [
        (group.id, group.kind, group.mode) for group in saved.speaker_groups
    ] == [(group.id, group.kind, group.mode) for group in before.speaker_groups]
    assert [
        (channel.role, channel.driver_style, channel.physical_output_index)
        for group in saved.speaker_groups
        for channel in group.channels
    ] == [
        (channel.role, channel.driver_style, channel.physical_output_index)
        for group in before.speaker_groups
        for channel in group.channels
    ]
    # The offer is spent: the save now matches the attached hardware.
    assert sound_setup._output_topology_payload()["hardware_repin"] is None


def test_repinned_box_reconcile_cannot_repoint_the_statefile_at_audio(
    monkeypatch, tmp_path: Path,
):
    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b="NEW-DONGLE")
    _stub_repin_runtime(monkeypatch)
    baseline = tmp_path / "baseline.yml"
    baseline.write_text(_active_baseline_yaml("stereo", 2))
    applied = tmp_path / "baseline.json"
    candidate = {"config": {"path": str(baseline)}}
    persist_applied_baseline_profile(candidate, apply_state={"result": "success"})
    statefile = tmp_path / "statefile.yml"
    parked = tmp_path / "parked.yml"
    decisions = []

    def reconcile(**_kwargs):
        topology = load_output_topology()
        decision = safe_graph_for_current_topology(
            topology,
            statefile_path=statefile,
            parked_config_path=parked,
            staged_metadata_path=tmp_path / "staged.json",
        )
        apply_safe_graph_decision_to_statefile(
            decision, statefile_path=statefile, topology=topology,
        )
        decisions.append(decision)
        return {"ok": True}

    reconcile()
    assert decisions[-1].status == "select_active_baseline"
    topology = load_output_topology()
    apply_safe_graph_decision_to_statefile(
        parked_safe_graph_decision(topology, config_path=parked),
        statefile_path=statefile, topology=topology,
    )
    monkeypatch.setattr("jasper.output_topology_runtime.trigger_reconcile", reconcile)

    sound_setup._repin_output_topology_payload({})

    assert not applied.exists()
    assert decisions[-1].status == PARKED_MUTED_STATUS
    assert decisions[-1].selected_config_path == str(parked)
    assert str(baseline) not in statefile.read_text()

    persist_applied_baseline_profile(candidate, apply_state={"result": "success"})
    reconcile()
    assert decisions[-1].status == "select_active_baseline"
    assert decisions[-1].selected_config_path == str(baseline)
    assert f"config_path: {baseline}" in statefile.read_text()


@pytest.mark.parametrize(
    ("reconcile", "status"),
    [
        (RECONCILE_STILL_CONVERGING, "converging"),
        (RECONCILE_FAILED, "needs_attention"),
    ],
)
def test_repin_reports_the_reconcile_verdict(
    monkeypatch, tmp_path: Path, reconcile, status,
):
    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b="NEW-DONGLE")
    _stub_repin_runtime(monkeypatch)
    _stub_reconcile(monkeypatch, reconcile)

    payload = sound_setup._repin_output_topology_payload({})

    assert payload["repin"]["status"] == status


def test_repin_refuses_when_the_attached_pair_is_already_pinned(
    monkeypatch,
    tmp_path: Path,
):
    """The endpoint is not a laxer door than the offer it is rendered from."""

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b=RIGHT_APPLE_SERIAL)
    assert sound_setup._output_topology_payload()["hardware_repin"] is None
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda *_args, **_kwargs: pytest.fail("unofferable re-pin must not park"),
    )

    with pytest.raises(sound_setup.OutputHardwareRequestConflict) as raised:
        sound_setup._repin_output_topology_payload({})

    assert raised.value.code == "repin_unavailable"


def test_sound_output_topology_repin_http_route_is_csrf_protected(
    monkeypatch,
    tmp_path: Path,
):
    calls = []
    monkeypatch.setattr(
        sound_setup,
        "_repin_output_topology_payload",
        lambda raw: calls.append(raw) or {"output_topology": {"status": "valid"}},
    )
    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(base, "/output-topology/repin", {})
        payload = json.loads(resp.read().decode("utf-8"))

        assert calls == [{}]
        assert payload["output_topology"]["status"] == "valid"


def _save_topology(monkeypatch, tmp_path: Path, raw: dict) -> Path:
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(OutputTopology.from_mapping(raw), path=topology_path)
    return topology_path


def _passive_stereo_topology_raw() -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "passive",
        "name": "Passive pair",
        "hardware": {"device_id": "hifiberry_dac8x", "physical_output_count": 8},
        "speaker_groups": [
            {
                "id": side,
                "label": side.title(),
                "kind": side,
                "mode": "full_range_passive",
                "channels": [{
                    "role": "full_range",
                    "physical_output_index": index,
                    "identity_verified": True,
                }],
            }
            for index, side in enumerate(("left", "right"))
        ],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    }


@pytest.mark.parametrize("review_ready", [False, True])
@pytest.mark.parametrize(("exists", "stands"), [(False, False), (True, True), (True, False)])
def test_tuning_handoff_follows_the_pages_applied_record(monkeypatch, review_ready, exists, stands):
    from jasper.active_speaker import tuning_handoff

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_v2.round_inputs.recent_round_sessions",
        lambda **_kwargs: [],
    )
    payload = tuning_handoff.build_tuning_handoff(
        commissioning_view={"programs": RUNNABLE_PROGRAMS, "review": {"ready": review_ready, "may_apply": review_ready}, "applied_profile": {
            "exists": exists, "stands": stands, "candidate_fingerprint": "applied-fp",
            "applied_at": "2026-09-13T12:00:00Z", "config_path": "/var/lib/camilladsp/applied.yml",
        }},
        design_draft={"revision": 5},
    )
    assert payload["status"] == ("ready" if stands else "not_ready")
    assert payload["reason"] == (None if stands else "no_applied_baseline")
    assert bool(payload["prompt"]) is stands
    assert payload["binding"]["hostname"] == "jts7.local"
    assert payload["binding"]["design_draft_revision"] == 5
    assert payload["binding"]["declaration_url"] == "http://jts7.local/sound/speaker/"


@pytest.mark.parametrize("program_id", ["speaker", "room", "bass"])
@pytest.mark.parametrize("has_applied", [False, True])
def test_tuning_handoff_prompt_binds_this_speaker_and_carries_no_credential(
    monkeypatch, program_id, has_applied,
):
    """Hostname-derived, revision-stamped, and closed against credentials.

    A prompt is minted to be pasted into a third-party chat session, so every
    field in it is disclosed by construction (non-negotiable 3) — hence a
    closed key set, not a scan for words. The default ``jts.local`` must never
    leak either: it resolves to *a* box, so a printed one sends its reader to
    the wrong speaker silently.
    """
    from jasper.active_speaker import tuning_handoff
    from jasper.identity.reader import DEFAULT_HOSTNAME

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.round_inputs.recent_round_sessions", lambda **_kwargs: [
        Path("/var/lib/jasper/active_speaker/campaigns/round-7")])
    applied = {
        "exists": has_applied, "stands": has_applied,
        "candidate_fingerprint": "applied-fp" if has_applied else None,
        "record": "record-12" if has_applied else None,
        "applied_at": "2026-09-13T12:00:00Z" if has_applied else None,
    }
    payload = tuning_handoff.build_tuning_handoff(
        commissioning_view={"programs": RUNNABLE_PROGRAMS, "applied_profile": applied},
        design_draft={"revision": 5},
        program_id=program_id,
    )
    prompt = tuning_handoff.build_tuning_handoff_prompt(payload["binding"], program_id)

    assert set(payload["binding"]) == {
        "speaker_name",
        "hostname",
        "declaration_url",
        "crossover_url",
        "design_draft_revision",
        "applied_candidate_fingerprint",
        "applied_record",
        "applied_at",
        "latest_round_dir",
    }
    document_positions = [prompt.index(entry["path"])
                          for entry in tuning_handoff.reading_order()]
    assert document_positions == sorted(document_positions)
    assert max(document_positions) < prompt.index("This speaker:")
    assert ("applied-fp" in prompt) is has_applied
    assert ("record-12" in prompt) is has_applied
    assert ("2026-09-13T12:00:00Z" in prompt) is has_applied
    assert ("no baseline applied" in prompt) is not has_applied
    assert "/var/lib/jasper/active_speaker/campaigns/round-7" in prompt
    assert "jts7.local" in prompt
    assert DEFAULT_HOSTNAME not in prompt
    assert str(payload["binding"]["design_draft_revision"]) in prompt
    # The pointer targets: the orientation verb and the program door, by their
    # installed paths. Their behaviour is theirs to own; the prompt only names
    # them, and must keep naming ones that exist.
    assert tuning_handoff.ORIENTATION_COMMAND in prompt
    assert tuning_handoff.PROGRAM_DOOR_COMMAND in prompt
    assert "jasper-round trial" not in prompt  # rear-only guidance
    assert len(prompt.split()) < 250


def test_tuning_handoff_prompt_for_rear_adds_the_trial_commands(monkeypatch):
    """Rear alone carries the trial/packet guidance, naming the real commands."""
    from jasper.active_speaker import tuning_handoff

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_v2.round_inputs.recent_round_sessions",
        lambda **_kwargs: [],
    )
    payload = tuning_handoff.build_tuning_handoff(
        commissioning_view={"programs": RUNNABLE_PROGRAMS, "applied_profile": {
            "exists": True, "stands": True, "candidate_fingerprint": "fp",
        }},
        design_draft={"revision": 1},
        program_id="rear",
    )
    prompt = tuning_handoff.build_tuning_handoff_prompt(payload["binding"], "rear")

    assert "--program rear" in prompt
    assert "--section rear" in prompt
    assert "jasper-round trial" in prompt


@pytest.mark.parametrize("banked", [True, False])
def test_tuning_handoff_names_the_round_directory_not_its_bundle(tmp_path, monkeypatch, banked):
    """A banked round is named by its own directory, the one every view and ``status`` take (#5632 F11)."""
    from jasper.active_speaker import tuning_handoff

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    round_dir = tmp_path / "campaigns" / "round-7"
    bundle = round_dir / "bundle" / "session-7" if banked else tmp_path / "sessions" / "session-7"
    bundle.mkdir(parents=True)
    (bundle / "info.json").write_text("{}")
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.round_inputs.recent_round_sessions",
                        lambda **_kwargs: [bundle])

    binding = tuning_handoff.build_tuning_handoff_binding({}, {})

    assert binding["latest_round_dir"] == str(round_dir if banked else bundle)


def test_tuning_handoff_route_serves_the_minted_payload(tmp_path, monkeypatch):
    from jasper.active_speaker import tuning_handoff

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.round_inputs.recent_round_sessions", lambda **_kwargs: [
        Path("/var/lib/jasper/active_speaker/campaigns/round-7")])
    monkeypatch.setattr(
        "jasper.active_speaker.commissioning_coordinator.load_commissioning_view",
        lambda *a, **k: {"programs": ("speaker", "bass", "room"), "applied_profile": {
            "exists": True, "stands": True, "candidate_fingerprint": "applied-fp",
            "record": "record-12", "applied_at": "2026-09-13T12:00:00Z",
        }},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft",
        lambda *a, **k: {"status": "ready_for_review", "revision": 2},
    )
    with sound_server(tmp_path) as base:
        with urllib.request.urlopen(base + "/active-speaker/tuning-handoff?program=room") as resp:
            assert resp.status == 200
            payload = json.loads(resp.read())
        with urllib.request.urlopen(base + "/active-speaker/tuning-handoff") as resp:
            default_payload = json.loads(resp.read())
        with pytest.raises(urllib.error.HTTPError) as invalid:
            urllib.request.urlopen(base + "/active-speaker/tuning-handoff?program=unknown")

    assert payload["status"] == "ready"
    assert payload["binding"]["design_draft_revision"] == 2
    assert tuple(entry["id"] for entry in payload["programs"]) == ("speaker", "bass", "room")
    assert all("prompt" not in entry for entry in payload["programs"])
    assert payload["program"] == "room"
    assert payload["prompt"] == tuning_handoff.build_tuning_handoff_prompt(
        payload["binding"], "room")
    assert default_payload["program"] == "speaker"
    assert default_payload["prompt"] == tuning_handoff.build_tuning_handoff_prompt(
        default_payload["binding"], "speaker")
    assert invalid.value.code == 400
    assert set(json.loads(invalid.value.read())) == {"error"}


@pytest.mark.parametrize("legacy_profile", [False, True])
def test_design_draft_get_computes_profile_from_current_values(monkeypatch, tmp_path, legacy_profile):
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.test_active_speaker_driver_safety import _manual_settings

    topology = mono_output_topology(card_id=None)
    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    path = paths["JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"]
    saved = sound_setup._active_speaker_design_draft_save_payload({"manual_settings": _manual_settings()})
    stored = json.loads(path.read_text())
    assert {"driver_safety_profile", "driver_safety_profile_evaluation", "driver_protection_policy_view"}.isdisjoint(stored)
    if legacy_profile:
        stored.update(driver_safety_profile={"targets": "obsolete"},
                      driver_safety_profile_evaluation={"status": "malformed"}, driver_protection_policy_view={"obsolete": True})
    stored["manual_settings"]["drivers"][1]["recommended_highpass_hz"] = 6000
    path.write_text(json.dumps(stored))
    before = path.read_bytes()
    loaded = sound_setup._active_speaker_design_draft_payload()
    assert "driver_safety_profile_evaluation" not in loaded
    profile = loaded["driver_safety_profile"]
    assert {"status", "confirmation", "profile_fingerprint", "research"}.isdisjoint(profile)
    tweeter = next(t for t in profile["targets"] if t["target_id"] == "mono:tweeter")
    assert tweeter["recommended_highpass_hz"] == 6000
    assert tweeter["hard_excitation_band_hz"][0] == 6000
    assert tweeter["required_protection_filters"][0]["cutoff_hz"] == 6000
    assert profile != saved["driver_safety_profile"]
    assert "obsolete" not in loaded["driver_protection_policy_view"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("reason", ["", "follower", "no_rear_output", "no_applied_profile", "no_rear_layer", "rear_muted_in_tune"])
def test_cardioid_compare_availability_contract(tmp_path, monkeypatch, reason):
    from types import SimpleNamespace
    from jasper.active_speaker import baseline_profile

    applied = {"applied_at": "2026-09-21T12:00:00Z", "recomposition_snapshot": {
        "rear_calibration": {"rear_muted": reason == "rear_muted_in_tune"},
        "linearization": {"woofer": [{}]},
    }}
    if reason == "no_rear_layer":
        del applied["recomposition_snapshot"]["rear_calibration"]
    if reason == "no_applied_profile":
        applied = None
    topology = SimpleNamespace(speaker_groups=[SimpleNamespace(channels=[
        SimpleNamespace(output_variant="primary" if reason == "no_rear_output" else "rear"),
    ])])
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: applied)
    monkeypatch.setattr(sound_active_speaker, "load_output_topology", lambda: topology)
    monkeypatch.setattr(sound_active_speaker, "bonded_follower_active", lambda: reason == "follower")
    monkeypatch.setattr(sound_active_speaker, "audition_summary", lambda: None)
    level = {"status": "matched", "trim_db": 1.2, "louder": "on", "reason": "",
             "round_id": "pair", "banked_at": "2026-09-20T12:00:00Z"}
    monkeypatch.setattr(sound_active_speaker, "rear_compare_level", lambda **kwargs: level)
    payload = _drive_compare_get(tmp_path, "/cardioid-compare")
    assert payload == {
        "available": not reason, "reason": reason, "state": "normal",
        "tune": {"label": "Current tune", "layers": [] if applied is None else
                 ["driver"] if reason == "no_rear_layer" else ["driver", "rear"],
                 "applied_at": applied["applied_at"] if applied else None},
        "level_match": level,
        "expires_in_s": None,
    }


@pytest.mark.parametrize("layer,state,seconds", [("rear_compare", "off", 42), ("rear_compare", "on", 1799), ("baseline", None, None)])
def test_cardioid_compare_session_disclosure(tmp_path, monkeypatch, layer, state, seconds):
    monkeypatch.setattr(sound_active_speaker, "audition_summary", lambda: {
        "layer": layer, "state": state, "expires_in_s": seconds,
    })
    payload = _drive_compare_get(tmp_path, "/cardioid-compare")
    assert payload["state"] == (state or "normal")
    assert payload["expires_in_s"] == seconds


@pytest.mark.parametrize("refusal,status", [(None, 200), ("audition_measurement_session_active", 409), ("audition_running_graph_differs", 409), ("audition_malformed_graph", 409), ("audition_restore_failed", 502)])
@pytest.mark.parametrize("state", ["off", "on", "normal"])
def test_cardioid_compare_post_contract(tmp_path, monkeypatch, refusal, status, state):
    from jasper.active_speaker.audition import AuditionRefused

    payload = {"available": state != "normal", "state": "normal", "level_match": {
        "status": "unavailable", "trim_db": None, "louder": None, "reason": "cache_miss"}}
    block = Mock(return_value=payload)
    monkeypatch.setattr(sound_setup.time, "time", lambda: 1000.0)
    calls, holders = [], []
    async def change(state, *, cam, trim_db):
        calls.append((state, trim_db))
        if refusal:
            raise AuditionRefused(refusal, "The compare could not change.")
        return {"status": "restored" if state == "normal" else "auditioning", "token": "session", "deadline_at": 1042.0}
    monkeypatch.setattr(sound_setup, "start_web_audition_holder", lambda record, *args: holders.append(record["token"]))
    monkeypatch.setattr(sound_setup, "_cardioid_compare_payload", block)
    monkeypatch.setattr(sound_setup, "set_compare_state", change)
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    body = json.dumps({"state": state}).encode()
    response, reads = _drive_raw_sound_post(tmp_path, path="/cardioid-compare", content_length=len(body), body=body)
    assert f" {status} ".encode() in response.split(b"\r\n", 1)[0]
    result = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert result.get("error") == refusal
    if refusal:
        assert set(result) == {"error", "message"}
    else:
        assert result == {**payload, "state": state, "expires_in_s": None if state == "normal" else 42}
    block.assert_called_once_with(cached_only=True)
    assert calls == [(state, 0.0)]
    assert holders == (["session"] if not refusal and state != "normal" else [])
    assert reads == [len(body)]


@pytest.mark.parametrize("louder", ["on", "off", None])
@pytest.mark.parametrize("state", ["on", "off", "normal"])
def test_cardioid_compare_passes_only_the_louder_states_trim(tmp_path, monkeypatch, louder, state):
    from unittest.mock import AsyncMock

    change = AsyncMock(return_value={"status": "restored"})
    monkeypatch.setattr(sound_setup, "set_compare_state", change)
    monkeypatch.setattr(sound_setup, "_cardioid_compare_payload", lambda **kwargs: {
        "available": True, "level_match": {"trim_db": 1.23, "louder": louder}})
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    body = json.dumps({"state": state}).encode()
    response, _ = _drive_raw_sound_post(tmp_path, path="/cardioid-compare", content_length=len(body), body=body)
    assert b" 200 " in response.split(b"\r\n", 1)[0]
    change.assert_awaited_once_with(state, cam=None, trim_db=1.23 if state == louder else 0.0)


@pytest.mark.parametrize("failure,reason", [("rear", "no_applied_rear"), ("round", "no_pair_round"),
    ("range", "delta_out_of_range"), ("preview", "preview_refused"), ("selector", "preview_refused"),
    ("applied", "level_error")])
def test_unavailable_level_never_blocks_compare(compare_evidence, tmp_path, monkeypatch, failure, reason):
    from unittest.mock import AsyncMock
    from jasper.active_speaker import rear_compare, state_paths
    from jasper.active_speaker.crossover_v2 import rear_preview, rear_pair_round as readers
    from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused

    if failure == "rear":
        compare_evidence[1]["recomposition_snapshot"].clear()
    elif failure == "round":
        monkeypatch.setattr(state_paths, "DEFAULT_CAMPAIGN_ROOT", tmp_path / "empty")
    elif failure == "range":
        monkeypatch.setattr(rear_preview, "rear_compare_delta_db", lambda preview: 6.01)
    elif failure == "preview":
        monkeypatch.setattr(rear_preview, "preview_rear_section", Mock(side_effect=RoundCapturesRefused("refused", {})))
    elif failure == "selector":
        monkeypatch.setattr(readers, "newest_rear_pair_round", Mock(side_effect=LookupError()))
    else:
        from jasper.active_speaker import baseline_profile
        monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", Mock(side_effect=OSError()))
    level = rear_compare.rear_compare_level()
    assert (level["status"], level["reason"], level["trim_db"], level["louder"]) == ("unavailable", reason, None, None)
    monkeypatch.setattr(sound_setup, "_cardioid_compare_payload", lambda **kwargs: {
        "available": True, "level_match": rear_compare.rear_compare_level(**kwargs)})
    change = AsyncMock(return_value={"status": "auditioning", "token": "session", "deadline_at": 9999999999.0})
    monkeypatch.setattr(sound_setup, "set_compare_state", change)
    monkeypatch.setattr(sound_setup, "start_web_audition_holder", Mock())
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    for state in ("off", "on"):
        body = json.dumps({"state": state}).encode()
        response, _ = _drive_raw_sound_post(tmp_path, path="/cardioid-compare", content_length=len(body), body=body)
        assert b" 200 " in response.split(b"\r\n", 1)[0]
        change.assert_awaited_with(state, cam=None, trim_db=0.0)


@pytest.mark.parametrize("failure", ["hold", "enter", "thread", "start"])
def test_compare_restores_if_holder_setup_fails(tmp_path, monkeypatch, failure):
    from unittest.mock import AsyncMock, MagicMock
    from jasper.active_speaker import audition

    events = []
    async def change(*args, **kwargs):
        events.append("installed")
        return {"status": "auditioning", "token": "session", "deadline_at": 9999999999.0}
    restore = AsyncMock(side_effect=lambda **kwargs: events.append("restored"))
    hold = MagicMock()
    if failure == "enter":
        hold.__enter__.side_effect = LookupError()
    monkeypatch.setattr(sound_setup, "no_hold", Mock(return_value=hold,
                       side_effect=LookupError() if failure == "hold" else None))
    if failure == "thread":
        monkeypatch.setattr(audition.threading, "Thread", Mock(side_effect=RuntimeError()))
    elif failure == "start":
        monkeypatch.setattr(audition.threading.Thread, "start", Mock(side_effect=RuntimeError()))
    monkeypatch.setattr(sound_setup, "set_compare_state", change)
    monkeypatch.setattr(audition, "stop_audition", restore)
    monkeypatch.setattr(sound_setup, "_cardioid_compare_payload", lambda **kwargs: {"available": True})
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    body = b'{"state":"off"}'
    response, _ = _drive_raw_sound_post(tmp_path, path="/cardioid-compare", content_length=len(body), body=body)
    assert b" 502 " in response.split(b"\r\n", 1)[0]
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["error"] == "audition_load_refused"
    restore.assert_awaited_once_with(cam=None, expect_token="session")
    assert events == ["installed", "restored"]
    assert hold.__exit__.call_count == int(failure in {"thread", "start"})


def test_cardioid_compare_post_unavailable_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setattr(sound_setup, "_cardioid_compare_payload", lambda **kwargs: {"available": False})
    body = b'{"state":"off"}'
    response, reads = _drive_raw_sound_post(tmp_path, path="/cardioid-compare", content_length=len(body), body=body)
    assert b" 403 " in response.split(b"\r\n", 1)[0]
    assert reads == []
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    response, _ = _drive_raw_sound_post(tmp_path, path="/cardioid-compare", content_length=len(body), body=body)
    assert b" 409 " in response.split(b"\r\n", 1)[0]
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["error"] == "cardioid_compare_unavailable"


def _drive_compare_get(tmp_path, path):
    handler_cls = sound_setup._make_handler(profile_path=tmp_path / "profile.json",
        library_path=tmp_path / "library.json", config_dir=tmp_path, camilla_factory=lambda: None)
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = io.BytesIO(f"GET {path} HTTP/1.1\r\nHost: jts.local\r\n\r\n".encode())
    handler.wfile = io.BytesIO()
    handler.client_address, handler.server = ("127.0.0.1", 0), None
    handler.raw_requestline = handler.rfile.readline()
    assert handler.parse_request()
    handler.do_GET()
    response = handler.wfile.getvalue()
    assert b" 200 " in response.split(b"\r\n", 1)[0]
    return json.loads(response.split(b"\r\n\r\n", 1)[1])


def test_cardioid_compare_absent_from_get_state(tmp_path, monkeypatch):
    block = Mock(side_effect=AssertionError())
    monkeypatch.setattr(sound_setup, "_cardioid_compare_payload", block)
    monkeypatch.setattr(sound_setup, "_eq_carrier_block", lambda *a, **k: None)
    assert "cardioid_compare" not in _drive_compare_get(tmp_path, "/state")
    block.assert_not_called()


@pytest.mark.parametrize("warm", [False, True])
def test_compare_post_never_selects_or_previews(compare_evidence, tmp_path, monkeypatch, warm):
    from unittest.mock import AsyncMock
    from jasper.active_speaker import rear_compare
    from jasper.active_speaker.crossover_v2 import rear_preview, rear_pair_round as readers

    monkeypatch.setattr(rear_preview, "preview_rear_section", lambda *a, **k: {})
    monkeypatch.setattr(rear_preview, "rear_compare_delta_db", lambda preview: 0.35)
    if warm:
        rear_compare.rear_compare_level()
        rear_compare._levels.clear()
    selector, preview = Mock(side_effect=AssertionError()), Mock(side_effect=AssertionError())
    monkeypatch.setattr(readers, "newest_rear_pair_round", selector)
    monkeypatch.setattr(rear_preview, "preview_rear_section", preview)
    level = Mock(wraps=rear_compare.rear_compare_level)
    def block(**kwargs):
        return {"available": True, "level_match": level(**kwargs)}
    payload = Mock(side_effect=block)
    monkeypatch.setattr(sound_setup, "_cardioid_compare_payload", payload)
    change = AsyncMock(return_value={"status": "auditioning", "deadline_at": 9999999999.0, "token": "session"})
    monkeypatch.setattr(sound_setup, "set_compare_state", change)
    monkeypatch.setattr(sound_setup, "start_web_audition_holder", Mock())
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    body = b'{"state":"on"}'
    response, _ = _drive_raw_sound_post(tmp_path, path="/cardioid-compare", content_length=len(body), body=body)
    assert b" 200 " in response.split(b"\r\n", 1)[0]
    result = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert result["level_match"]["status"] == ("matched" if warm else "unavailable")
    change.assert_awaited_once_with("on", cam=None, trim_db=0.35 if warm else 0.0)
    payload.assert_called_once_with(cached_only=True)
    level.assert_called_once_with(cached_only=True)
    selector.assert_not_called()
    preview.assert_not_called()


def test_cardioid_compare_uses_follower_post_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    responses = []
    for route in ("/cardioid-compare", "/live-draft"):
        response, reads = _drive_raw_sound_post(tmp_path, path=route, content_length=2, body=b"{}")
        assert b" 409 " in response.split(b"\r\n", 1)[0]
        assert reads == []
        responses.append(json.loads(response.split(b"\r\n\r\n", 1)[1]))
    assert responses[0] == responses[1]
    assert set(responses[0]) == {"error"}


def test_sound_server_construction_starts_no_thread(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    from jasper.platform import systemd

    start = Mock(side_effect=AssertionError())
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(systemd, "make_http_server", lambda target, handler: SimpleNamespace(RequestHandlerClass=handler))
    sound_setup.make_server(("127.0.0.1", 0), profile_path=tmp_path / "profile.json",
                            library_path=tmp_path / "library.json", config_dir=tmp_path)
    start.assert_not_called()


def test_web_startup_recovers_after_installing_idle_hold(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from contextlib import contextmanager
    from jasper.web import __main__ as web_main

    events = []
    class Handler:
        pass
    class Tracker:
        @contextmanager
        def hold(self, label):
            events.append("hold")
            try:
                yield
            finally:
                events.append("release")
        def start(self):
            events.append("tracker_start")
    tracker = Tracker()
    server = SimpleNamespace(RequestHandlerClass=Handler, serve_forever=lambda: events.append("serve"))
    spec = replace(next(s for s in web_main.WIZARD_SPECS if s.label == "/sound"),
                   make_server=lambda target: server)
    async def recover(cam):
        assert Handler.idle_hold == tracker.hold
        assert events == ["hold"]
        events.append("recover")
    monkeypatch.setattr(web_main, "recover_web_audition", recover)
    monkeypatch.setattr(web_main, "primary_controller", lambda: object())
    monkeypatch.setattr(web_main, "_specs_for_role", lambda role: (spec,))
    monkeypatch.setattr(web_main, "_active_install_role", lambda: "speaker")
    monkeypatch.setattr("jasper.volume_process.install_env_canonical_target_provider", lambda: None)
    monkeypatch.setattr(web_main._systemd, "IdleShutdownTracker", lambda: tracker)
    monkeypatch.setattr(web_main._systemd, "adopt_systemd_sockets", lambda: [])
    monkeypatch.setattr(web_main._systemd, "install_request_idle_bump", lambda *args: None)
    monkeypatch.setattr(web_main._systemd, "notify_ready", lambda: None)
    monkeypatch.setattr(web_main._systemd, "notify_stopping", lambda: None)
    assert web_main.main() == 0
    assert events == ["hold", "recover", "release", "tracker_start", "serve"]


async def test_live_draft_retires_compare_record(tmp_path, monkeypatch):
    from jasper.active_speaker import audition
    from tests.test_camilla_controller import _controller, _FakeClient

    _configure_passive_layout_for_eq(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    _record_dsp_epoch(tmp_path / "dsp.json", "epoch-1")
    current = tmp_path / "sound_current.yml"
    current.write_text(_room_config([PeqFilter(freq=80.0, q=4.0, gain=-3.0)]))
    record = tmp_path / "audition.json"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_AUDITION_STATE", str(record))
    record.write_text(json.dumps({"kind": audition.AUDITION_STATE_KIND, "schema_version": 1,
        "layer": "rear_compare", "state": "off", "token": "session", "owner_pid": 123,
        "deadline_at": 9999999999.0}))
    assert sound_active_speaker._cardioid_compare_payload()["state"] == "off"
    client = _FakeClient()
    client.parse_yaml = lambda text: text
    cam = _controller(client, tmp_path)
    async def anchor(**kwargs):
        return str(current)
    monkeypatch.setattr(cam, "get_config_file_path", anchor)
    monkeypatch.setattr("jasper.camilla.MAIN_VOLUME_RAMP_SETTLE_S", 0.0)
    payload = await sound_setup._live_draft_profile(
        SoundProfile(curve_id="harman", simple_eq=SimpleEq(bass_db=2.0)),
        expected_dsp_write_epoch=dsp_write_epoch(), config_dir=tmp_path,
        camilla_factory=lambda: cam,
    )
    assert payload["live_status"] == "live"
    assert len(client.active_raw_values) == 1
    assert not record.exists()
    assert sound_active_speaker._cardioid_compare_payload()["state"] == "normal"


@pytest.mark.parametrize('layout,crossover,channels,cardioid,count', [
    ('mono', 'passive', 2, False, 1), ('stereo', 'passive', 2, False, 2),
    ('mono', 'active', 2, False, 2), ('stereo', 'active', 2, False, 4),
    ('mono', 'active', 3, False, 3), ('stereo', 'active', 3, False, 6),
    ('mono', 'active', 3, True, 3), ('stereo', 'active', 3, True, 6),
])
def test_setup_layout_choices_build_distinct_driver_outputs(layout, crossover, channels, cardioid, count):
    from jasper.active_speaker.layout import build_speaker_layout, layout_choices
    from jasper.active_speaker.measurement_programs import programs_for_topology
    from tests.active_speaker_fixtures import mono_output_topology

    choices = dict(layout=layout, crossover=crossover, channels=channels, cardioid=cardioid)
    topology = build_speaker_layout(mono_output_topology(), choices)
    targets = [channel.target_id(group.id) for group in topology.speaker_groups for channel in group.channels]
    outputs = [channel.physical_output_index for group in topology.speaker_groups for channel in group.channels]
    assert len(set(targets)) == len(set(outputs)) == count
    assert topology.evaluation()['status'] == 'valid'
    assert layout_choices(topology) == choices
    assert ('rear' in programs_for_topology(topology)) is cardioid


def test_setup_research_import_uses_one_draft_writer_and_preserves_edits(tmp_path, monkeypatch):
    from jasper.web import sound_speaker_setup as setup
    from jasper.active_speaker.design_inputs import resolved_draft_inputs
    from jasper.active_speaker.design_draft import load_design_draft, save_design_draft
    from jasper.active_speaker.driver_safety import build_driver_research_context
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.test_active_speaker_driver_safety import _operator_inputs, _research_result

    topology = mono_output_topology()
    path = tmp_path / 'draft.json'
    monkeypatch.setenv('JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE', str(path))
    monkeypatch.setattr(setup, 'load_output_topology', lambda: topology)
    inputs = _operator_inputs()
    target = topology.speaker_groups[0].channels[0].target_id(topology.speaker_groups[0].id)
    edits = {'drivers': [{'target_id': target, 'role': 'woofer', 'gain_offset_db': -3,
                         'cabinet': {'enclosure_kind': 'sealed'}}]}
    save_design_draft(topology, operator_inputs=inputs, manual_settings=edits)
    context = build_driver_research_context(topology, inputs, edits)
    assert context['targets'][0]['installation']['enclosure_kind'] == 'sealed'
    assert context['targets'][0]['installation']['pad'] == {'kind': 'none'}
    assert 'gain_offset_db' not in context['targets'][0]['installation']
    research = _research_result(context)
    research['drivers'][0]['sensitivity_db_2v83_1m'] = 85
    setup.import_research({'text': 'Result:\n```json\n' + json.dumps(research) + '\n```'})
    research['drivers'][0]['sensitivity_db_2v83_1m'] = 86
    setup.import_research({'text': json.dumps(research)})
    draft = load_design_draft(topology=topology)
    resolved = resolved_draft_inputs(draft)['drivers'][0]
    assert resolved['sensitivity_db_2v83_1m'] == 86
    assert resolved['gain_offset_db'] == -3
    assert resolved['gain_offset_db_provenance'] == 'operator_pinned'
    assert resolved['cabinet']['enclosure_kind'] == 'sealed'
    assert 'sensitivity_db_2v83_1m' not in draft['manual_settings']['drivers'][0]
    before = path.read_bytes()
    for bad in ('{', json.dumps({**research, 'drivers': [{**row, 'model': 'Wrong speaker'} for row in research['drivers']]})):
        with pytest.raises(ValueError):
            setup.import_research({'text': bad})
        assert path.read_bytes() == before
    inputs = {**inputs, 'target_models': {target: 'Replacement woofer'}}
    setup.save_details({'operator_inputs': inputs, 'manual_settings': edits})
    draft = load_design_draft(topology=topology)
    assert draft['driver_research'] is None
    assert draft['manual_settings']['drivers'][0]['gain_offset_db'] == -3


def test_setup_preserves_unambiguous_legacy_trim_when_other_bindings_are_ambiguous(tmp_path, monkeypatch):
    from jasper.web import sound_speaker_setup as setup
    from jasper.active_speaker import baseline_profile
    from jasper.active_speaker.design_draft import save_design_draft
    from jasper.active_speaker.design_inputs import resolved_draft_inputs
    from jasper.active_speaker.layout import build_speaker_layout
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.test_active_speaker_driver_safety import _operator_inputs

    topology = build_speaker_layout(mono_output_topology(), {
        "layout": "mono", "crossover": "active", "channels": 3, "cardioid": True,
    })
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(tmp_path / "draft.json"))
    monkeypatch.setattr(setup, "load_output_topology", lambda: topology)
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: None)
    monkeypatch.setattr(setup.commissioning_coordinator, "load_commissioning_view", lambda topology: {
        "programs": programs_for_topology(topology), "applied_profile": {"stands": False},
        "driver_values": {"complete": False}, "review": {"issues": []},
    })
    inputs = _operator_inputs()
    save_design_draft(topology, operator_inputs=inputs, manual_settings={"drivers": [
        {"role": "woofer", "gain_offset_db": -2}, {"role": "tweeter", "gain_offset_db": -20},
    ]})
    manual = setup.load_setup_view()["draft"]["manual_settings"]
    for group in topology.speaker_groups:
        for channel in group.channels:
            target = channel.target_id(group.id)
            if not any(row.get("target_id") == target for row in manual["drivers"]):
                manual["drivers"].append({"target_id": target, "role": channel.role})
    setup.save_details({"operator_inputs": inputs, "manual_settings": manual})
    draft = load_design_draft(topology=topology)
    drivers = resolved_draft_inputs(draft)["drivers"]
    assert next(row for row in drivers if row["role"] == "tweeter")["gain_offset_db"] == -20
    assert any(row.get("target_id") is None and row["role"] == "woofer" and row["gain_offset_db"] == -2
               for row in draft["manual_settings"]["drivers"])


@pytest.mark.parametrize('style', ['', 'compression_driver'])
def test_setup_partial_details_return_research_action_without_measurement_errors(tmp_path, monkeypatch, style):
    from jasper.web import sound_speaker_setup as setup
    from jasper.active_speaker import baseline_profile
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.test_active_speaker_driver_safety import _operator_inputs

    topology = mono_output_topology()
    target = topology.speaker_groups[0].channels[1].target_id(topology.speaker_groups[0].id)
    saved_layouts = []
    def save_layout(raw):
        nonlocal topology
        topology = type(topology).from_mapping(raw['output_topology'])
        saved_layouts.append(topology)
        return {'save': {'status': 'saved'}}
    monkeypatch.setattr(sound_active_speaker, '_save_output_topology_payload', save_layout)
    monkeypatch.setenv('JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE', str(tmp_path / 'draft.json'))
    monkeypatch.setattr(setup, 'load_output_topology', lambda: topology)
    monkeypatch.setattr(baseline_profile, 'load_applied_baseline_profile_state', lambda: None)
    monkeypatch.setattr(setup.commissioning_coordinator, 'load_commissioning_view', lambda topology: {
        'programs': programs_for_topology(topology),
        'applied_profile': {'stands': False}, 'driver_values': {'complete': False},
        'review': {'issues': [{'code': 'measurement_band_missing'}]},
    })
    response = setup.update_setup('/setup/details', {
        'operator_inputs': _operator_inputs(), 'manual_settings': {}, 'driver_styles': {target: style},
    }, camilla_factory=lambda: None)
    view = response['setup']
    assert view['stage'] == 'research'
    assert view['next_action']['id'] == 'copy_research'
    assert view['draft']['prompt']
    assert len(saved_layouts) == bool(style)
    assert view['draft']['targets'][1]['driver_style'] == (style or None)
    assert view['issues'] == []
    assert view == setup.load_setup_view()
    assert [p['id'] for p in view['programs']] == ['speaker', 'bass', 'room']


def test_setup_apply_uses_declared_base_instead_of_the_incumbent(tmp_path, monkeypatch):
    from jasper.web import sound_speaker_setup as setup, sound_active_speaker
    from tests.test_correction_crossover_v2_endpoints import _seed_baseline_apply_environment

    _seed_baseline_apply_environment(monkeypatch, tmp_path)
    seen = []
    async def apply(**kwargs):
        seen.append(kwargs['candidate'])
        return {'status': 'applied'}
    monkeypatch.setattr(sound_active_speaker, '_active_speaker_finish_commissioning_payload', apply)
    monkeypatch.setattr(setup, 'load_setup_view', lambda: {'stage': 'tune', 'programs': [{'id': 'speaker'}]})
    response = setup.update_setup('/setup/apply', {}, camilla_factory=lambda: None)
    assert response['result']['status'] == 'applied'
    assert response['setup'] == setup.load_setup_view()
    assert seen[0].analysis['measurement_status'] == 'unmeasured'
    assert not seen[0].linearization


def test_speaker_setup_browser_contract():
    if _NODE is None:
        pytest.skip("node not on PATH")
    result = subprocess.run([_NODE, str(Path(__file__).parent / 'js/speaker_setup_test.mjs')], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('path,writer', [('/setup/save-layout', '_save_output_topology_payload'),
                                      ('/setup/reset', '_reset_output_topology_payload')])
def test_setup_routes_call_the_existing_sync_topology_writer(tmp_path, monkeypatch, path, writer):
    from jasper.web import sound_speaker_setup as setup

    async def audio_operation():
        return {'status': 'saved'}
    def save(raw):
        return asyncio.run(audio_operation())
    monkeypatch.setattr(sound_active_speaker, writer, save)
    monkeypatch.setattr(setup, 'load_setup_view', lambda: {'stage': 'details'})
    with sound_server(tmp_path) as base:
        response = json.loads(json_post_with_csrf(base, path, {}).read())
    assert response['result']['status'] == 'saved'
    assert response['setup']['stage'] == 'details'
