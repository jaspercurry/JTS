# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Integration and contract tests for the household sound setup surface."""

from __future__ import annotations

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
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.playback_route import OUTPUTD_ACTIVE_LANE_SOURCE
from jasper.camilla_config_contract import PeqFilter
from jasper.dsp_apply import DspApplyState, dsp_write_epoch, record_dsp_apply_state
from jasper.output_topology import (
    CROSS_CHILD_GROUP_CODE,
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    OutputTopologyError,
    load_output_topology,
    output_topology_mutation,
    set_channel_identity_verified,
)
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
from jasper.web import sound_setup, volume_floor_tone

from .active_speaker_fixtures import (
    PASSIVE_ONLY_DAC_ID,
    PASSIVE_ONLY_DAC_LABEL,
    register_passive_only_dac,
)
from ._web_test_helpers import (
    json_post_with_csrf,
    make_csrf_session,
    request_with_csrf,
)


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
    """The ONE ``event=<name> k=v …`` record, plus its fields as a mapping.

    Asserting on fields rather than the whole sentence keeps these pins off
    log prose, and folding "exactly one record" in here makes that property
    explicit at every call site.
    """

    records = [
        record
        for record in caplog.records
        if record.getMessage().split(" ", 1)[0] == f"event={event}"
    ]
    assert len(records) == 1, [record.getMessage() for record in records]
    fields = dict(
        token.split("=", 1)
        for token in records[0].getMessage().split(" ")[1:]
        if "=" in token
    )
    return records[0], fields


def _stub_audio_stops(monkeypatch, stops: list[str] | None = None) -> list[str]:
    """Stub the three audio sessions a topology mutation stops before parking.

    Records ``"<summed|commission>:<reason>"`` per tone and ``"safe"`` for the
    safe-playback session, in call order, so a test can pin the ordering.
    """

    recorded = [] if stops is None else stops

    def stop_tone(kind: str):
        def _stop(*, reason: str) -> dict:
            recorded.append(f"{kind}:{reason}")
            return {"status": "idle", "reason": reason}

        return _stop

    def stop_safe() -> dict:
        recorded.append("safe")
        return {"status": "idle"}

    monkeypatch.setattr(
        sound_setup, "_active_speaker_stop_summed_test_tone", stop_tone("summed")
    )
    monkeypatch.setattr(
        sound_setup, "_active_speaker_stop_commission_tone", stop_tone("commission")
    )
    monkeypatch.setattr(sound_setup, "_active_speaker_stop_payload", stop_safe)
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

    from jasper.output_topology import OutputTopology, save_output_topology

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


class FakeCamilla:
    """Enough of CamillaDSP 4.1 to tell a parameter write from a pipeline
    replace: it keeps the graph it is running, hands it back the way
    CamillaDSP's readback does, and normalizes a candidate through the same
    round-trip so the two are comparable — which is the property
    ``jasper.sound.live_edit.plan_live_edit_for`` depends on.
    """

    def __init__(self, current_path: str, *, fail_set: bool = False) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None
        self.set_calls: list[str] = []
        self.active_raw_values: list[str] = []
        self.ducks: list[bool] = []
        self.fail_set = fail_set
        self.running: str | None = None

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.set_calls.append(path)
        self.loaded_path = path
        if self.fail_set and not best_effort:
            raise RuntimeError("reload failed")
        return True

    @staticmethod
    def _normalized(text: str) -> str:
        import yaml as _yaml

        return str(_yaml.safe_dump(_yaml.safe_load(text)))

    async def set_active_config_raw(
        self, config: str, *, best_effort: bool = False, duck: bool = True,
    ) -> bool:
        self.running = self._normalized(config)
        self.active_raw_values.append(config)
        self.ducks.append(duck)
        if self.fail_set and not best_effort:
            raise RuntimeError("live update failed")
        return True

    async def get_active_config_raw(self, *, best_effort: bool = False) -> str | None:
        return self.running

    async def normalize_config_raw(
        self, config: str, *, best_effort: bool = False,
    ) -> str | None:
        return self._normalized(config)


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
_ACTIVE_SPEAKER_UI_MODULE = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "sound-profile" / "js" / "active-speaker-ui.js"
)
_SOUND_CSS = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "sound-profile" / "sound.css"
)
_SOUND_HARNESS = Path(__file__).resolve().parent / "js" / "sound_profile_harness.mjs"
_ACTIVE_SPEAKER_UI_TEST = (
    Path(__file__).resolve().parent / "js" / "active_speaker_ui_test.mjs"
)
_ACTIVE_SPEAKER_UI_JS = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "assets"
    / "sound-profile"
    / "js"
    / "active-speaker-ui.js"
)
_NODE = shutil.which("node")


def test_active_speaker_ui_level_match_helpers():
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_ACTIVE_SPEAKER_UI_TEST)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True


def test_commission_load_refuses_while_a_measurement_runs(monkeypatch):
    """commission-load serializes against room correction / balance / sync: when
    one is active it refuses with a distinct reason (not a camilla touch) so the
    UI shows the correct message instead of "another driver is being tested"."""
    from jasper.web import active_speaker_flow

    monkeypatch.setattr(
        active_speaker_flow, "blocking_measurement_phase", lambda: "correction:sweeping"
    )

    def _camilla_must_not_be_called():
        raise AssertionError("camilla_factory must not run when the load is refused")

    payload = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "main", "role": "woofer"},
            camilla_factory=_camilla_must_not_be_called,
        )
    )
    assert payload["status"] == "refused"
    assert payload["reason"] == "measurement_in_progress"
    assert payload["blocking_phase"] == "correction:sweeping"


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
    ("error", "error_type"),
    [
        (OSError("commission abort I/O failed"), "OSError"),
        (RuntimeError("commission abort runtime failed"), "RuntimeError"),
    ],
)
def test_commission_ramp_abort_http_contains_secondary_failures(
    tmp_path,
    monkeypatch,
    caplog,
    error,
    error_type,
):
    async def fail_abort(*, camilla_factory):
        raise error

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_commission_ramp_abort_payload",
        fail_abort,
    )
    caplog.set_level(logging.ERROR, logger=sound_setup.logger.name)
    with sound_server(tmp_path) as base:
        response = json_post_with_csrf(
            base,
            "/active-speaker/commission-ramp-abort",
            {},
            expect_status=502,
        )
        assert response.headers.get_content_type() == "application/json"
        payload = json.loads(response.read().decode("utf-8"))

    assert payload == {"error": str(error)}
    record, fields = _event_record(caplog, "sound.post_dispatch_failed")
    assert fields == {
        "path": "/active-speaker/commission-ramp-abort",
        "error_type": error_type,
    }
    assert record.exc_info is not None
    assert record.exc_info[0] is type(error)


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
            "POST",
            "/active-speaker/channel-protection",
            "_active_speaker_channel_protection_save_payload",
            "sound.active_speaker_channel_protection",
            {"error": "OSError"},
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
    """A route whose payload builder raises answers `{"error": …}` at 502 and
    emits exactly one `result=error` event named for that route."""
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
    assert _event_record(caplog, event)[1] == {"result": "error", **extra_fields}


def test_sound_post_does_not_secondary_send_after_response_write_failure(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(sound_setup, "guard_mutating_request", lambda _handler: True)
    monkeypatch.setattr(sound_setup, "_active_speaker_stop_payload", lambda: {"ok": True})
    response_sink = _BrokenPipeBytesIO()
    caplog.set_level(logging.ERROR, logger=sound_setup.logger.name)

    with pytest.raises(BrokenPipeError, match="synthetic client disconnect"):
        _drive_raw_sound_post(
            tmp_path,
            path="/active-speaker/stop",
            content_length=0,
            body=b"",
            response_sink=response_sink,
        )

    assert response_sink.write_calls == 1
    assert not any(
        record.getMessage().startswith("event=sound.post_dispatch_failed")
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("content_length", "expected_error"),
    [
        (-1, "Content-Length must not be negative"),
        (sound_setup.MAX_JSON_BYTES + 1, "JSON body exceeds 65536 bytes"),
    ],
)
def test_sound_post_rejects_invalid_body_length_before_read(
    tmp_path,
    monkeypatch,
    content_length,
    expected_error,
):
    monkeypatch.setattr(sound_setup, "guard_mutating_request", lambda _handler: True)

    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/active-speaker/commission-ramp-abort",
        content_length=content_length,
    )

    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert json.loads(response.split(b"\r\n\r\n", 1)[1]) == {
        "error": expected_error,
    }
    assert read_calls == []


def test_sound_post_unknown_route_precedes_csrf_and_body_read(tmp_path, monkeypatch):
    def fail_if_guarded(_handler):
        raise AssertionError("unknown route must return before the CSRF guard")

    monkeypatch.setattr(sound_setup, "guard_mutating_request", fail_if_guarded)

    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/not-a-sound-route",
        content_length=-1,
    )

    assert b" 404 " in response.split(b"\r\n", 1)[0]
    assert read_calls == []


def test_stale_sound_stop_and_abort_routes_still_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(sound_setup, "guard_mutating_request", lambda _handler: True)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_payload",
        lambda: {"route": "stop"},
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_summed_test_tone",
        lambda *, reason: {"route": "summed-stop", "reason": reason},
    )

    async def abort(*, camilla_factory):
        return {"route": "commission-abort"}

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_commission_ramp_abort_payload",
        abort,
    )

    expected = {
        "/active-speaker/stop": "stop",
        "/active-speaker/commission-ramp-abort": "commission-abort",
        "/active-speaker/summed-test/stop": "summed-stop",
    }
    for path, route in expected.items():
        response, read_calls = _drive_raw_sound_post(
            tmp_path,
            path=path,
            content_length=2,
            body=b"{}",
        )
        assert b" 200 " in response.split(b"\r\n", 1)[0]
        assert json.loads(response.split(b"\r\n\r\n", 1)[1])["route"] == route
        assert read_calls == [2]


def test_sound_post_csrf_rejection_precedes_body_read(tmp_path, monkeypatch):
    guard_calls = []

    def reject(_handler):
        guard_calls.append("guard")
        return False

    monkeypatch.setattr(sound_setup, "guard_mutating_request", reject)

    response, read_calls = _drive_raw_sound_post(
        tmp_path,
        path="/i2s-hat",
        content_length=-1,
    )

    assert b" 403 " in response.split(b"\r\n", 1)[0]
    assert guard_calls == ["guard"]
    assert read_calls == []

    monkeypatch.setattr(sound_setup, "guard_mutating_request", lambda _handler: True)
    body = b'{"enabled":1}'
    response, read_calls = _drive_raw_sound_post(
        tmp_path, path="/i2s-hat", content_length=len(body), body=body
    )
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert read_calls == [len(body)]


# The EQ chrome the /sound/setup/ page must not render: the Off/Saved/Draft
# tablist and the now-playing plot are content-DSP surfaces.
_EQ_ONLY_CHROME = ('id="tab-off"', 'id="tab-saved"', 'id="tab-draft"', 'id="plot"')


@pytest.mark.parametrize(
    ("page_mode", "title"),
    [("eq", "EQ"), ("setup", "Sound setup")],
)
def test_index_html_renders_the_page_shell_for_its_mode(page_mode, title):
    """Both modes share the design system and the static module; only the EQ
    mode renders the Off/Saved/Draft chrome, and neither inlines logic."""
    html = sound_setup._index_html(page_mode=page_mode).decode()

    assert "/assets/app.css" in html
    assert "/assets/sound-profile/sound.css?v=" in html  # linked, not inlined
    assert "<style>" not in html
    assert f'class="app-header__title">{title}' in html
    assert 'id="sound-page-data"' in html
    assert f'"mode": "{page_mode}"' in html
    # The editor is a static ES module (served + revalidated by nginx), the
    # same delivery model as /system/, with no inline logic left in the page.
    assert '<script type="module" src="/assets/sound-profile/js/main.js">' in html
    assert "<script>" not in html

    if page_mode == "eq":
        assert all(marker in html for marker in _EQ_ONLY_CHROME[:3])
    else:
        assert 'id="view-body"' in html
        assert not any(marker in html for marker in _EQ_ONLY_CHROME)


def _island_payload(html: str) -> dict:
    marker = 'id="sound-page-data"'
    start = html.index(">", html.index(marker)) + 1
    return json.loads(html[start : html.index("</script>", start)])


@pytest.mark.parametrize("follower", [False, True])
def test_sound_page_island_serves_the_compilers_crossover_vocabulary(
    monkeypatch, follower,
):
    """The page offers exactly what the compiler builds, and is TOLD what that is.

    Before this the filter picker carried its own list (including a Butterworth
    the compiler refuses) and the slope was a free ``step="6"`` field, so the
    editor could author a crossover that only failed at
    ``crossover_preview_filter_unsupported`` several screens later. Deriving the
    offer here is what makes widening
    :data:`~jasper.active_speaker.profile.SUPPORTED_CROSSOVER_TYPES` /
    ``SUPPORTED_LR_ORDERS`` reach the wizard with no edit in main.js. A bonded
    follower keeps its LOCAL driver domain, so it gets the same offer.
    """
    from jasper.active_speaker.crossover_preview import (
        DEFAULT_FILTER_TYPE,
        DEFAULT_SLOPE_DB_PER_OCTAVE,
    )
    from jasper.active_speaker.declaration_vocabulary import (
        supported_declaration_filter_types,
        supported_declaration_slopes_db_per_octave,
    )

    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: follower)
    island = _island_payload(sound_setup._index_html(page_mode="setup").decode())
    vocabulary = island["crossover_vocabulary"]

    assert island["follower"] is follower
    assert vocabulary["filter_types"] == list(supported_declaration_filter_types())
    assert vocabulary["slopes_db_per_octave"] == list(
        supported_declaration_slopes_db_per_octave()
    )
    assert vocabulary["default_filter_type"] == DEFAULT_FILTER_TYPE
    assert vocabulary["default_slope_db_per_octave"] == DEFAULT_SLOPE_DB_PER_OCTAVE


def test_design_draft_save_refuses_an_uncompilable_crossover_at_the_door(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The refusal reaches the operator's own surface, not just the module.

    A save carrying a filter the compiler cannot build must come back 400 with
    the field named and the offer spelled out, instead of being persisted and
    then blocked at ``crossover_preview_filter_unsupported`` several screens
    later.
    """
    from jasper.output_topology import output_topology_mutation
    from tests.active_speaker_fixtures import mono_output_topology

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "active_speaker_design_draft.json"),
    )
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    with output_topology_mutation(topology_path) as mutation:
        mutation.save(mono_output_topology(card_id=None))
    with sound_server(tmp_path) as base:
        refused = json_post_with_csrf(
            base,
            "/active-speaker/design-draft",
            {
                "expected_revision": 0,
                "operator_inputs": {"woofer": "W", "tweeter": "T"},
                "manual_settings": {
                    "crossover_candidates": [
                        {
                            "between_roles": ["woofer", "tweeter"],
                            "frequency_hz": 2500,
                            "filter_type": "Butterworth",
                            "slope_db_per_octave": 24,
                        }
                    ]
                },
            },
            expect_status=400,
        )
        payload = json.loads(refused.read().decode("utf-8"))

    assert payload["error"] == (
        "crossover_candidate.filter_type must be one of: Linkwitz-Riley"
    )


def test_eq_page_delegates_content_dsp_when_bonded_follower(monkeypatch):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    leader_paths = []
    monkeypatch.setattr(
        sound_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": leader_paths.append(path) or "http://jts3.local/eq/",
    )

    html = sound_setup._index_html("csrf-token", page_mode="eq").decode()

    # The delegation card stays: content EQ / room correction / volume shaping
    # are the leader's job while paired.
    assert "Sound is controlled by the pair leader" in html
    assert leader_paths == ["/eq/"]
    assert "http://jts3.local/eq/" in html
    assert 'href="/sound/setup/">Open local sound setup</a>' in html
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


def test_setup_page_keeps_local_commissioning_when_bonded_follower(monkeypatch):
    monkeypatch.setattr(sound_setup, "bonded_follower_active", lambda: True)
    leader_paths = []
    monkeypatch.setattr(
        sound_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": leader_paths.append(path) or "http://jts3.local/sound/setup/",
    )

    html = sound_setup._index_html("csrf-token", page_mode="setup").decode()

    assert leader_paths == ["/sound/setup/"]
    assert "http://jts3.local/sound/setup/" in html
    assert 'id="view-body"' in html
    assert "/assets/sound-profile/js/main.js" in html
    assert '"mode": "setup"' in html
    assert '"follower": true' in html
    assert 'id="tab-off"' not in html
    assert 'id="plot"' not in html


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
            _follower_get_status(base, "/active-speaker/safe-playback", session) == 200
        )
        # An active-speaker mutation reaches its handler (200/502), never the
        # follower 409 nor a 404 — the gate is content-DSP only.
        active_status = _follower_post_status(
            base, "/active-speaker/stage-config", session,
        )
        assert active_status not in (404, 409), active_status


def test_summed_test_level_http_route_is_registered(monkeypatch, tmp_path: Path):
    """The live combined-test slider route must pass the route-before-CSRF gate."""

    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", None)
    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(
            base,
            "/active-speaker/summed-test/level",
            {"speaker_group_id": "main", "level_dbfs": -35},
        )
        payload = json.loads(resp.read().decode("utf-8"))
        assert payload["status"] == "idle"
        assert payload["reason"] == "no_active_summed_test"


def test_index_html_embeds_csrf_meta_for_json_posts():
    html = sound_setup._index_html("csrf-token").decode()
    # The token rides in the meta tag; the static module reads it and sends
    # X-CSRF-Token on every mutating POST.
    assert 'meta name="jts-csrf" content="csrf-token"' in html


def test_sound_active_speaker_ui_helpers_are_pure_module_boundary():
    js = _ACTIVE_SPEAKER_UI_MODULE.read_text()

    assert "export function activeSpeakerStepState" in js
    assert "export function defaultActiveSpeakerStep" in js
    assert "export function playbackResultMessage" in js
    assert "querySelector" not in js
    assert "document." not in js
    assert "fetch(" not in js
    assert "explicit lab backend" not in js


def test_sound_module_preserves_editor_behaviour():
    """The EQ editor moved from inline _SOUND_JS into a static module. Guard
    the load-bearing pieces so the relocation can't silently drop them: the
    5-band Simple field names, the backend endpoints + epoch handshake, the
    CSRF-via-meta wiring, and no legacy prompt() flow."""
    js = _SOUND_MODULE.read_text()
    assert "sub_bass_db" in js
    assert "presence_db" in js
    for path in (
        "./preview", "./live-draft", "./apply",
        "./profiles/save", "./profiles/rename", "./profiles/delete",
        "./volume-floor/audition", "./volume-floor/stop",
    ):
        assert path in js, f"sound module no longer references {path}"
    assert "dsp_write_epoch: dspWriteEpoch" in js
    assert "function cancelLiveDrafts()" in js
    assert "jsonHeaders()" in js
    # CSRF/JSON helpers are imported from the shared http.js (which reads the
    # meta[name=jts-csrf] tag) rather than re-declared locally — the token is
    # never string-substituted at render time. (See the http.js drift guard in
    # tests/test_web_wizard_conventions.py.)
    assert 'from "/assets/shared/js/http.js"' in js
    assert "Active crossover setup" in js
    assert "/assets/sound-profile/js/active-speaker-ui.js" in js
    assert "./active-speaker/prepare-driver-test" not in js
    assert "./active-speaker/measurements" in js
    assert "./active-speaker/baseline-profile" in js
    assert "./output-topology" in js
    assert "./output-topology/reset" in js
    assert "./output-topology/repin" in js
    assert "Reset speaker setup" in js
    assert "Keep setup, pin the new DAC" in js
    assert "Test combined drivers" in js
    assert "Validate and apply" in js
    assert "Save and apply" in js
    assert "Choose layout, set crossover values, confirm outputs" in js
    assert "Start tone" in js
    assert "Stop tone" in js
    assert "Save floor" in js
    assert "save-volume-floor" in js
    assert "Confirm outputs" in js
    assert "Back to adjust crossover" in js
    assert "Reset floor" in js
    assert "reset-volume-floor" in js
    assert "function saveVolumeFloor" in js
    assert "function setVolumeFloorDraft" in js
    assert "return true;" in js
    assert "return false;" in js
    assert "else if (act === 'save-volume-floor')" in js
    assert "setVolumeFloorDraft(floor);" in js
    assert "await saveVolumeFloor();" in js
    assert "pagehide" in js
    assert "scheduleVolumeFloorToneUpdate(volumeFloorValue());" in js
    assert "if (volumeFloorTone.active) scheduleVolumeFloorToneUpdate(volumeFloorValue(), {immediate: true});" in js
    assert "stopVolumeFloorTone({keepalive: true, quiet: true, reason: 'pagehide'})" in js
    assert "function defaultOutputStep()" in js
    assert "return defaultActiveSpeakerStep(outputStepContext(currentOutputTopology()));" in js
    helper_js = _ACTIVE_SPEAKER_UI_MODULE.read_text()
    assert "if (!ctx.driverResearchSatisfied) return 'research';" in helper_js
    assert "if (!driverTargetProofComplete) return 'map';" in helper_js
    assert "ctx.driverChecksComplete || ctx.driverMeasurementsComplete" in helper_js
    assert "if (!ctx.summedValidationComplete) return 'safety';" in helper_js
    assert "return 'profile';" in helper_js
    assert "Finish the current card before opening" in js
    assert "output-step__chevron" in js
    assert "querySelectorAll('.output-step[open]')" in js
    assert "window.prompt" not in js


def test_i2s_hat_payload_reports_modes_and_truthful_restart(monkeypatch, tmp_path):
    intent = tmp_path / "i2s_hat.env"
    marker = tmp_path / "i2s-reboot"
    marker.touch()
    hardware = {
        "profile_id": "unknown",
        "status": "partial",
        "child_devices": [{"device_id": "innomaker_hifi_amp_pro"}],
        "usb_data_role": {"board_topology": "shared_otg_port"},
    }
    monkeypatch.setattr(sound_setup, "I2S_HAT_REBOOT_REQUIRED_PATH", str(marker))
    monkeypatch.setattr(sound_setup, "_output_hardware_dict", lambda: hardware)

    payload = sound_setup._i2s_hat_payload(intent_path=intent)

    assert payload["desired_enabled"] is False
    assert payload["runtime_active"] is True
    assert payload["restart_required"] is True
    assert payload["visibility"] == "visible"
    assert payload["available"] is True
    assert payload["shared_usb_data_port"] is True
    assert all(text in _SOUND_MODULE.read_text() for text in ("Enabling this setting reserves this shared port for gadget/peripheral use after restart, so it can no longer host a USB output DAC and output moves to the HAT.", "ordinary powered micro-USB host cable: it supplies 5 V", "Use a VBUS-isolated data connection/adapter or leave the port disconnected."))

    hardware.clear()
    hardware["usb_data_role"] = {"board_topology": "unsupported"}
    assert sound_setup._i2s_hat_payload(intent_path=intent)["available"] is False


def test_i2s_hat_save_reuses_start_only_reconcile_broker(monkeypatch):
    from jasper.control import restart_broker

    calls = []
    monkeypatch.setattr(
        sound_setup,
        "_i2s_hat_payload",
        lambda: {"available": True, "reason": "", "restart_required": True},
    )
    monkeypatch.setattr(
        sound_setup,
        "write_i2s_hat_intent",
        lambda enabled: calls.append(("write", enabled)),
    )

    def manage(unit, **kwargs):
        calls.append((unit, kwargs))
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", manage)

    payload, result = sound_setup._save_i2s_hat_payload(True)

    assert calls[0] == ("write", True)
    unit, options = calls[1]
    assert unit == "jasper-audio-hardware-reconcile.service"
    assert options["verb"] == "start"
    assert options["no_block"] is False
    assert options["timeout"] == 55.0

    def fail_apply(*_args, **_kwargs):
        raise OSError("broker unavailable")

    monkeypatch.setattr(restart_broker, "manage_units", fail_apply)
    refreshed, failed = sound_setup._save_i2s_hat_payload(False)
    assert refreshed["restart_required"] is True
    assert failed == {"ok": False, "error": "broker unavailable"}

    repo = Path(__file__).resolve().parents[1]
    unit = (repo / "deploy/systemd/jasper-audio-hardware-reconcile.service").read_text()
    nginx = (repo / "deploy/nginx-jasper.conf").read_text()
    proxy = nginx.split("location /sound/setup/", 1)[1].split("}", 1)[0]
    streambox_proxy = (repo / "deploy/nginx-jasper-streambox.conf").read_text().split("location /sound/setup/", 1)[1].split("}", 1)[0]
    assert "TimeoutStartSec=50s" in unit
    assert 50 < 55 < 55 + restart_broker._CLIENT_SOCKET_MARGIN_SEC < 65
    assert "proxy_read_timeout 65s;" in proxy
    assert "proxy_read_timeout 65s;" in streambox_proxy


def test_sound_module_active_speaker_status_is_explicit_read_only():
    js = _SOUND_MODULE.read_text()

    assert 'from "/assets/sound-profile/js/active-speaker-ui.js"' in js
    assert "function refreshActiveSpeakerStatus()" not in js
    for retired in (
        "fetch('./active-speaker/environment'",
        "fetch('./active-speaker/safe-playback'",
        "fetch('./active-speaker/staged-config'",
        "fetch('./active-speaker/prepare-driver-test'",
        "fetch('./active-speaker/stage-config'",
        "fetch('./active-speaker/check-path-safety'",
        "fetch('./active-speaker/load-startup-config'",
        "fetch('./active-speaker/rollback-startup-config'",
        "fetch('./active-speaker/play-tone'",
        "fetch('./active-speaker/floor-audio-result'",
        "fetch('./active-speaker/driver-measurement'",
        "fetch('./active-speaker/startup-load'",
        "function activeSpeakerPost(",
        "function stopActiveSpeakerTest()",
        "fmtDbfs",
        "driverResearchDraftSaved",
        "toneSummary",
        "outputStartupLoaded",
        "quietTestStartupReady",
        "quietTestStagedReady",
        "driverCheckRolesForGroup",
        "driverMeasurementCounts",
        "fetchActiveSpeakerStartupLoad",
        "stagedConfig",
        "startupLoad",
        "data-act=\"stop-active-speaker\"",
        "data-act=\"active-floor-result\"",
        "data-act=\"check-output-readiness\"",
        "data-act=\"play-output-readiness-tone\"",
        "action: 'auto_step'",
    ):
        assert retired not in js
    assert "'./active-speaker/commission-load'" in js
    assert "'./active-speaker/commission-ramp-step'" in js
    assert "'./active-speaker/commission-ramp-ack'" in js
    assert "'./active-speaker/commission-ramp-abort'" in js
    assert "'./active-speaker/commissioning-view'" in js
    assert "action && action.endpoint || './active-speaker/summed-test'" in js
    assert "action && action.endpoint || './active-speaker/summed-validation'" in js
    assert "fetch('./active-speaker/design-draft'" in js
    assert "fetch('./active-speaker/crossover-preview'" in js
    assert "fetch('./active-speaker/measurements'" in js
    assert "fetch('./active-speaker/baseline-profile'" in js
    assert "fetch('./active-speaker/baseline-profile/save-and-apply'" in js
    assert "expected_candidate_fingerprint: expectedCandidateFingerprint" in js
    assert "fetch('./active-speaker/baseline-profile/apply'" not in js
    assert "data-act=\"refresh-active-speaker\"" not in js
    assert "act: 'save-driver-design'" in js
    assert "act === 'prepare-crossover-preview'" in js
    assert "Save values" in js
    assert "Preview crossover" in js
    assert "function driverResearchCanPreparePreview()" in js
    assert "function driverResearchStepSatisfied()" in js
    assert "function driverResearchFlowComplete(topology)" in js
    assert "driverResearchSatisfied: driverResearchFlowComplete(topology)" in js
    assert "function commissioningStepView(step)" in js
    assert "function commissioningCurrentStep()" in js
    assert "if (!ctx.driverResearchSatisfied) return 'research';" in (
        _ACTIVE_SPEAKER_UI_MODULE.read_text()
    )
    assert "Preview crossover before confirming outputs." in js
    assert "Save driver names and crossover points before confirming outputs." in js
    assert "Working setup updated. No filters are active and no sound was played." in js
    assert "The working values below are ready to save and turn into a no-audio preview." in js
    assert "data-act=\"arm-active-speaker\"" not in js
    assert "data-act=\"stage-active-config\"" not in js
    assert "data-act=\"check-active-path-safety\"" not in js
    assert "active-speaker/check-path-safety" not in js
    assert "data-act=\"load-active-startup\"" not in js
    assert "data-act=\"rollback-active-startup\"" not in js
    assert "data-act=\"prepare-active-tone\"" not in js
    assert "data-act=\"verify-active-tone\"" not in js
    assert "activeSpeaker.playback" not in js
    assert "var activeSpeakerSetupOpen = false;" in js
    assert "'<details class=\"advanced\" data-active-speaker-setup' + (open ? ' open' : '')" in js
    assert "activeSpeakerSetupOpen = !!ev.target.open;" in js
    assert "No active driver test" not in js
    assert "no separate direct-DAC driver test in the product UI" not in js
    assert "id=\"active-speaker-level\"" not in js
    assert "data-act=\"active-level\"" not in js
    assert "Back to quiet" not in js
    assert "Raise toward audible" not in js
    assert "Mic reading dBFS" not in js
    assert "data-act=\"active-auto-level\"" not in js
    assert "activeSpeakerAutoLevelLabel(autoLevel)" not in js
    assert "action: 'observe'" not in js
    assert "action: 'auto_step'" not in js
    assert "Normal listening volume is untouched" not in js
    assert "The mic reading helps JTS decide whether to hold, lower, or raise" not in js
    assert "Normal listening volume is untouched" not in js
    assert "if (requestedLevel != null) body.level_dbfs = requestedLevel" not in js
    assert "level_dbfs: requestedLevel == null ? cfg.value : requestedLevel" not in js
    assert "function combinedTestLevelConfig()" in js
    assert "Combined test level" in js
    assert "body.level_dbfs = requestedLevel" in js
    assert "operator_listening_check: true" in js
    assert "Test combined drivers" in js
    assert "function baselineProfileRevalidation()" in js
    assert "Revalidate crossover blend" in js
    assert "Revalidation is saved. Save and apply a fresh active profile." in js
    assert "Your active speaker setup changed after the current profile was applied." in js
    assert "By-ear" not in js
    assert "Status" in js
    assert "auto_retry_pending" in js
    assert "silentAutoRetry" not in js
    assert "active-speaker/prepare-driver-test" not in js
    assert "syncPreparedOutputTopology(payload)" not in js
    assert "fetch('./active-speaker/stage-config'" not in js
    assert "fetch('./active-speaker/check-path-safety'" not in js
    assert "fetch('./active-speaker/load-startup-config'" not in js
    assert "Choose first driver" not in js
    assert "Listen for this driver" not in js
    assert "Exit test setup" not in js
    assert "No sound played. ' + e.message" not in js
    assert "active-speaker-actions--driver-test" not in js
    assert "data-act=\"record-summed-validation\"" in js
    assert "data-act=\"compile-baseline-profile\"" not in js
    assert "data-act=\"apply-baseline-profile\"" not in js
    assert "data-act=\"save-apply-baseline-profile\"" in js
    assert "I hear " in js
    assert "I did not hear anything" not in js
    assert "Wrong driver" not in js
    assert "Too loud / stop" not in js
    assert "Measure with mic" not in js
    assert "record-summed-capture" not in js
    assert "record-driver-capture" not in js
    assert "driver-test result" not in js
    assert "Did this driver make the sound?" not in js
    assert "If you hear nothing, wait" not in js
    assert "active-speaker/check-path-safety" not in js
    assert "Exit driver test setup and restore the previous DSP setup?" not in js
    assert "function renderActiveSpeakerPlan(plan)" not in js
    assert "function renderActiveSpeakerPlayback(playback)" not in js
    assert "Would play" not in js
    assert "Verify tone artifact" not in js
    assert "No audio was emitted by this backend." not in js
    assert "No preset channel targets available." not in js
    assert ">Prepare channel test</button>" not in js
    assert "No sound is playing" not in js


def test_sound_module_output_topology_surface_is_no_audio_and_backend_owned():
    js = _SOUND_MODULE.read_text()

    assert "function renderOutputTopologySetup()" in js
    assert "function refreshOutputTopology(options)" in js
    assert "function saveOutputTopology(options)" in js
    assert "function setOutputChannelAssignment(groupId, role, rawValue)" in js
    assert "function updateOutputChannelIdentity(button)" in js
    assert "function saveOutputChannelProtectionState(groupId, role, nextStatus)" not in js
    assert "function outputReadinessFromPreparedDriver(payload, button)" not in js
    assert "function checkOutputPlaybackReadiness(button)" not in js
    assert "fetch('./output-topology'" in js
    assert "fetch('./active-speaker/channel-identity'" in js
    assert "fetch('./active-speaker/channel-protection'" not in js
    assert "fetch('./active-speaker/prepare-driver-test'" not in js
    assert "fetch('./active-speaker/playback-readiness'" not in js
    assert 'data-act="mark-output-identity"' in js
    assert 'data-act="check-output-readiness"' not in js
    assert 'data-protection-required="' not in js
    assert 'data-protection-status="' not in js
    assert "headers: jsonHeaders()" in js
    assert "Saved speaker layout." in js
    # The map-step footer's dirty-layout fallback wires the save-layout action
    # through the shared descriptor renderer (renderStepFooterButton emits the
    # data-act attribute at runtime) rather than an inline data-act string.
    assert "act: 'save-output-topology'" in js
    assert "else if (act === 'save-output-topology')" in js
    assert "data-output-channel" in js
    assert "Assign each driver to one DAC channel. Play starts quiet and ramps." in js
    assert "Play each quiet ramp, then confirm the driver you hear." in js
    assert "Multi-DAC aggregate" in js
    assert "Composite clock" in js
    assert "observedHardware" in js
    assert "topology_revision" in js
    assert "resp.status === 409" in js
    assert "channelCount" in js
    assert "Hardware details" in js
    assert "Hardware mismatch" in js
    assert "Use detected hardware" in js
    assert "hardwareAdoption" in js
    assert "outputTopology.loading || outputTopology.saving || outputTopology.resetting" in js
    assert "detected_hardware_identity" in js
    # #2812 B1: the mismatch message is computed once, server-side, in
    # jasper.output_topology.declared_hardware_mismatch and published as
    # payload.hardware_mismatch; the page reads that field rather than
    # recomputing "Saved topology expects ..." locally.
    assert "hardwareMismatch" in js
    assert "Saved topology expects" not in js
    assert "Detected output hardware" not in js
    assert "supported" in js
    assert "needs attention" not in js
    assert "Confirm output" in js
    assert "'✓ ' + humanRole(channel.role) + ' confirmed'" not in js
    assert "'Test ' + humanRole(channel.role)" not in js
    assert "JTS will add the tweeter guard before any sound starts" in js
    assert "Getting ' + label + ' ready. No sound will play yet." not in js
    assert "Hardware protected" not in js
    assert "Use software guard" not in js
    assert "software_guard_requested" in js
    assert 'data-act="mark-output-protection"' not in js
    assert "function updateOutputChannelProtection(button)" not in js
    assert "Check readiness" not in js
    assert "Playback readiness" not in js
    assert "Change protection" not in js
    assert "Change quiet-start" not in js
    assert "Use quiet-start" not in js
    assert "Use for first test" not in js
    assert "protected startup DSP" not in js
    assert "function renderOutputReadinessSummary(readiness)" not in js
    assert "function renderOutputReadinessBlockers(readiness)" not in js
    assert "function outputCurrentLevelAtFloor()" not in js
    assert "function outputFloorAudioConfirmedForReadiness(readiness)" not in js
    assert "function quietStartTargetLabel(target)" not in js
    assert "function readinessTargetLockReason(readiness)" not in js
    assert "How to continue" not in js
    assert "This driver cannot be tested from here yet. Choose a woofer, mid, or subwoofer driver to continue." not in js
    assert "Starting with the quietest short pulse." not in js
    assert "Heard ' + targetLabel" not in js
    assert "Test progress" not in js
    assert "Role policy" not in js
    assert "Preconditions passed" not in js
    assert "Generate WAV (no sound)" not in js
    assert ">Start quiet test</button>" not in js
    assert ">I hear this driver</button>" not in js
    assert "No active driver test" not in js
    assert "no separate direct-DAC driver test in the product UI" not in js
    assert "playbackResultMessage(playback, undefined, friendlySetupReason)" not in js
    assert "Playback: ' + (issue.code" not in js
    assert "JTS could not get the test ready. No sound was played." not in js
    assert "Save this speaker layout draft before confirming outputs." in js
    assert "Main speakers" in js
    assert "Speaker count" in js
    assert "Speaker type" in js
    assert "var outputTemplateDraftAxes = {layout: '', speakerMode: ''};" in js
    assert "function outputTemplateChoiceDisabled(count, axis, value, axes)" in js
    assert "function outputTemplateUnavailableReason(template, topology, hasSubwoofer)" in js
    assert "This install can test and apply up to " in js
    assert "Subwoofer active profiles are not available on this install yet." in js
    assert "Choose passive, active 2-way, or active 3-way to continue." in js
    assert "Refresh hardware to start a speaker layout." in js
    assert "renderOutputHardwareRefresh() +" in js
    assert "Test combined drivers" in js
    assert "data-act=\"output-template-axis\"" in js
    assert "output-template-grid" not in js
    assert "Save output map" not in js
    assert "Output setup" not in js
    assert "Mono active 2-way" in js
    assert "Stereo active 3-way" in js
    assert "Speaker layout is a draft." in js
    assert "active-speaker-issues--warning" in js
    assert "renderIssueList(issues, 5)" in js
    # #2812 B1: the dual-Apple clock-blocker filter lives server-side in
    # jasper.output_topology.declared_hardware_mismatch (the same function
    # backing payload.hardware_mismatch above), not here.
    assert "function outputClockHardwareBlockers()" not in js
    assert "dual_apple_observed_" not in js
    assert "dual_apple_usb_topology_mismatch" not in js
    assert "function crossoverPreviewDisplayStatus(payload)" in js
    assert "JTS still checks the setup before any sound." in js
    assert "Starter stereo" not in js
    assert "Starter 2-way" not in js
    assert "protection_status: tweeter ? 'required_missing' : 'not_required'" in js
    assert "Saved speaker layout." in js


# --------------------------------------------------------------------------
# Commissioning blocker copy — the COMPLETENESS guards (#2344).
#
# These walk the BACKEND's codes, so a newly minted blocker with no renderer
# entry fails here rather than shipping.
#
# NOT asserted: "every backend blocker code has its own entry in every map".
# Of 78 literal codes, 46 have no specific mapping by design — the preparation
# failures out of `staging.py` collapse into the `commissioning_candidate_prepared`
# gate's one sentence, which is the right household copy for all of them.
# --------------------------------------------------------------------------

_COMMISSION_CODE_MODULES = (
    "jasper/active_speaker/staging.py",
    "jasper/active_speaker/commission_load.py",
    "jasper/active_speaker/startup_load.py",
    "jasper/active_speaker/commission_ramp.py",
    "jasper/active_speaker/web_commissioning.py",
    # Reachable through the same payloads: the preflight aggregates path-safety
    # and topology blockers, and tone playback contributes its own.
    "jasper/active_speaker/path_safety.py",
    "jasper/active_speaker/playback.py",
    "jasper/active_speaker/safe_playback.py",
    "jasper/active_speaker/driver_protection.py",
    "jasper/active_speaker/measurement.py",
    "jasper/web/sound_setup.py",
    # Also in the issue flow, and absent from this list until #2285: the
    # baseline emitter mints two of the four `ring_wire_declaration_invalid`
    # sites, the environment probe contributes the config/ALSA blockers the
    # preflight aggregates, and topology_tone's six target blockers are spliced
    # straight into the combined-test payload
    # (`web_commissioning._summed_target_or_issue`). A module that mints a
    # blocker a household can reach and is not scanned here is a guard that
    # reports on a subset while reading as complete.
    "jasper/active_speaker/baseline_profile.py",
    "jasper/active_speaker/environment.py",
    "jasper/active_speaker/topology_tone.py",
)

# An operator remedy: a sudo/systemctl invocation, a `jasper-*` binary, or a
# long CLI flag. Household surfaces must never carry one.
#
# The left boundary admits a backtick and a slash, not whitespace alone:
# backend prose spells a remedy as "Arm it with
# `sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile …`", where `sudo`
# follows a backtick and the binary follows a slash, so a whitespace-only
# boundary scans that whole sentence as command-free.
_OPERATOR_COMMAND_RE = re.compile(
    r"(?:^|[\s`/])(?:sudo\s|systemctl\s|jasper-[a-z-]+|--[a-z][a-z-]+)"
)


def _lit_str(node) -> str | None:
    """The literal string a node carries, or None when it is computed."""
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string: keep the literal parts
        return "".join(
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def _blocker_dict_fields(node) -> tuple[object, object] | None:
    """``(code_node, message_node)`` for a literal ``severity: "blocker"`` dict."""
    import ast

    if not isinstance(node, ast.Dict):
        return None
    fields = {
        key.value: value
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    if _lit_str(fields.get("severity")) != "blocker":
        return None
    if "code" not in fields:
        return None
    return fields["code"], fields.get("message")


# The seeded builders, defined OUTSIDE the scanned modules
# (`jasper.active_speaker._common`), so nothing here can discover them. ARITY,
# not name, disambiguates the `_issue` alias: most modules import
# `issue as _issue` (3 positional args, severity first) while
# `web_commissioning` imports `blocker_issue as _issue` (2 args, code first),
# so reading `args[1]` blindly would collect MESSAGES as codes there.
# Value is (code_index, message_index).
_SEEDED_BLOCKER_BUILDERS: dict[tuple[str, int], tuple[int, int]] = {
    ("_issue", 2): (0, 1),
    ("blocker_issue", 2): (0, 1),
    ("_blocked", 2): (0, 1),
}


def _local_blocker_helpers(tree) -> dict[str, tuple[int, int]]:
    """Module-local helpers that forward their own params into a blocker issue.

    Discovered BY DEFINITION, not by name: a hard-coded name list cannot be
    widened ahead of the next helper somebody writes, and a definition scan can.

    Returns ``name -> (code_arg_index, message_arg_index)`` for any function
    whose body builds a blocker (a literal `severity: "blocker"` dict, or a call
    to a seeded builder) directly out of its own positional parameters.
    """
    import ast

    helpers: dict[str, tuple[int, int]] = {}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [arg.arg for arg in func.args.args + func.args.kwonlyargs]

        def _param_index(node, _params=params):
            if isinstance(node, ast.Name) and node.id in _params:
                return _params.index(node.id)
            return None

        for inner in ast.walk(func):
            fields = _blocker_dict_fields(inner)
            if fields is None and isinstance(inner, ast.Call):
                target = inner.func
                inner_name = target.id if isinstance(target, ast.Name) else None
                key = (inner_name, min(len(inner.args), 2))
                if inner_name and key in _SEEDED_BLOCKER_BUILDERS and inner.args:
                    code_i, message_i = _SEEDED_BLOCKER_BUILDERS[key]
                    if len(inner.args) > max(code_i, message_i):
                        fields = (inner.args[code_i], inner.args[message_i])
            if fields is None:
                continue
            code_index = _param_index(fields[0])
            message_index = _param_index(fields[1])
            if code_index is not None and message_index is not None:
                helpers[func.name] = (code_index, message_index)
                break
    return helpers


def _commission_blocker_pairs() -> list[tuple[str, str, str]]:
    """AST-walk the commissioning modules for literal (code, message, where).

    Three shapes are collected: the seeded `_common` builders (by arity, see
    `_SEEDED_BLOCKER_BUILDERS`), any module-local helper that forwards its own
    params into a blocker, and bare `{"severity": "blocker", ...}` dict literals.

    Codes built from an f-string or a variable are invisible here — a known
    bound: the property under test is the literal prose an author writes next to
    a literal code, and a computed code carries a computed message the walk
    could not check either.
    """
    import ast

    repo = Path(__file__).resolve().parent.parent
    pairs: list[tuple[str, str, str]] = []
    for rel in _COMMISSION_CODE_MODULES:
        path = repo / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        builders = dict(_SEEDED_BLOCKER_BUILDERS)
        builders.update(
            {(name, 2): indices for name, indices in _local_blocker_helpers(tree).items()}
        )
        # The 3-arg severity-first alias, kept separate: its severity must be
        # the literal "blocker" before the pair counts.
        for node in ast.walk(tree):
            code = message = None
            fields = _blocker_dict_fields(node)
            if fields is not None:
                code, message = _lit_str(fields[0]), _lit_str(fields[1])
            elif isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else None
                )
                if name == "_issue" and len(node.args) >= 3:
                    if _lit_str(node.args[0]) == "blocker":
                        code, message = _lit_str(node.args[1]), _lit_str(node.args[2])
                else:
                    indices = builders.get((name, min(len(node.args), 2)))
                    if indices and len(node.args) > max(indices):
                        code = _lit_str(node.args[indices[0]])
                        message = _lit_str(node.args[indices[1]])
                if code is None:
                    kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                    if "code" in kwargs:
                        code = _lit_str(kwargs["code"])
                        message = _lit_str(kwargs.get("message"))
            if code:
                pairs.append((code, message or "", f"{rel}:{node.lineno}"))
    return pairs


def test_every_commissioning_blocker_carrying_a_command_is_mapped():
    """A blocker whose MESSAGE tells an operator what to run must have household copy.

    The summed-test surface is the one that can print a backend message verbatim:
    `summed_test_failure_message` falls through to `_household_safe_reason`, which
    strips absolute paths and exception classes but has no reason to know about
    shell commands — #2344's blocker ("Release it first with
    `jasper-active-speaker baseline-reemit --endpoint aloop`") passed it cleanly.
    So the rule is: if a blocker message carries an operator remedy, the household
    gets WRITTEN copy for that code instead of the raw sentence.
    """
    from jasper.active_speaker.commissioning_coordinator import (
        _SUMMED_TEST_FAILURE_COPY,
    )

    pairs = _commission_blocker_pairs()
    assert len(pairs) > 50, "the AST walk found almost nothing — this guard is vacuous"
    mapped = {code for code, _ in _SUMMED_TEST_FAILURE_COPY}

    unmapped = sorted(
        f"{code} ({where})"
        for code, message, where in pairs
        if _OPERATOR_COMMAND_RE.search(message) and code not in mapped
    )
    assert not unmapped, (
        "these blockers tell an operator what to run, but have no household copy, "
        "so the raw sentence — shell command and all — is what a household would "
        f"read on the combined-test card: {unmapped}"
    )


def test_no_commissioning_blocker_leaks_an_operator_command_to_a_household():
    """The property itself, not the proxy: what the household would actually see.

    Runs the REAL `_household_safe_reason` over every unmapped blocker message
    and asserts nothing survives it carrying a command. This is the assertion the
    map-coverage test above is a cheaper stand-in for, and it stays honest if the
    sanitiser is ever taught (or untaught) something.
    """
    from jasper.active_speaker.commissioning_coordinator import (
        _SUMMED_TEST_FAILURE_COPY,
        _household_safe_reason,
    )

    mapped = {code for code, _ in _SUMMED_TEST_FAILURE_COPY}
    leaks = []
    for code, message, where in _commission_blocker_pairs():
        if code in mapped:
            continue
        shown = _household_safe_reason(message)
        if shown and _OPERATOR_COMMAND_RE.search(shown):
            leaks.append(f"{code} ({where}): {shown!r}")
    assert not leaks, f"a household would be shown an operator command: {leaks}"


def test_every_preflight_gate_id_has_household_copy():
    """Every gate the preflight PUBLISHES has copy — the map is the closed set.

    `commissionGateReason` is the /sound/ renderer's fallback when no issue code
    matched, and it keys on the gate id. The preflight's top-level
    `required_gates` is the only list the renderer walks, so a gate added there
    without copy degrades silently to "A setup step still needs finishing" —
    which is how #2344's transport gate would have rendered.
    """
    import ast

    repo = Path(__file__).resolve().parent.parent
    source = (repo / "jasper" / "active_speaker" / "commission_load.py").read_text()
    tree = ast.parse(source)
    builder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_driver_commission_load_preflight"
    )
    published = set()
    for node in ast.walk(builder):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if name != "_gate" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            published.add(arg.value)
        elif isinstance(arg, ast.Name):
            # A gate id held in a module constant (the shared transport gate id
            # imported from staging). Resolve it through the module namespace
            # rather than re-parsing its source.
            from jasper.active_speaker import commission_load

            value = getattr(commission_load, arg.id, None)
            assert isinstance(value, str), (
                f"preflight gate id {arg.id!r} is not a resolvable module "
                "constant; this walk would silently skip it"
            )
            published.add(value)
    assert len(published) >= 5, f"gate walk found too few ids: {published}"

    helper_js = _ACTIVE_SPEAKER_UI_MODULE.read_text()
    gate_block = helper_js.split("export function commissionGateReason")[1].split(
        "}[gateId]"
    )[0]
    rendered = set(re.findall(r"^\s{4}([a-z0-9_]+):", gate_block, re.M))
    missing = sorted(published - rendered)
    assert not missing, (
        "these preflight gates would render the generic 'a setup step still "
        f"needs finishing' instead of telling the household what is wrong: {missing}"
    )


def test_the_transport_blockers_are_registered_on_every_household_surface():
    """Both directions of the shared code set (#2344, re-pointed by #2412).

    Each live code present on both surfaces, and the retired ring-transport
    rung absent from both: asserting only the first half would pass over a
    partial re-point that left the dead rung reachable.
    """
    from jasper.active_speaker.commissioning_coordinator import (
        _SUMMED_TEST_FAILURE_COPY,
    )
    from jasper.active_speaker.staging import COMMISSIONING_TRANSPORT_GATE_ID

    helper_js = _ACTIVE_SPEAKER_UI_MODULE.read_text()
    mapped = {code for code, _ in _SUMMED_TEST_FAILURE_COPY}
    # The codes the BUILDERS actually raise, so a map entry cannot outlive a
    # renamed emitter: the ends-disagree code carries no operator command, so
    # the command guard above would not catch a misspelling in its emitter.
    emitted = {code for code, _, _ in _commission_blocker_pairs()}
    # Quoted tokens, not substrings: `..._unarmed_X` contains `..._unarmed`, so
    # a substring assertion passes over a misspelled rung. The ladder's real
    # coverage is asserted behaviourally in tests/js/active_speaker_ui_test.mjs;
    # this is the cheap second layer, not the guarantee.
    for code in (
        "commissioning_transport_ends_disagree",
        "commissioning_ring_feed_unarmed",
        "commissioning_active_endpoint_unarmed",
        "ring_wire_declaration_invalid",
        "commission_live_state_stale",
        "commission_output_hardware_reconcile_failed",
        "commission_tone_backend_failed",
        "commission_active_graph_not_staged",
    ):
        assert code in emitted, f"no commissioning builder raises {code}"
        assert f"'{code}'" in helper_js, (
            f"the /sound/ issue ladder does not name {code}"
        )
        assert code in mapped, f"the combined-test card has no copy for {code}"
    # The three the ladder reaches through a PREFIX rung, never by name: its
    # `indexOf('commission_startup_anchor_')` branch collapses all three into
    # one sentence, while Python routes them to three families with three
    # remedies. Pinned as emitted + copied + swallowed by a rung that exists —
    # deliberately NOT pinned as equivalent advice, because it is not.
    assert "indexOf('commission_startup_anchor_') === 0" in helper_js, (
        "the /sound/ ladder no longer collapses the startup-anchor family by "
        "prefix; if it now names the codes, pin them like the eight above"
    )
    for code in (
        "commission_startup_anchor_not_staged",
        "commission_startup_anchor_path_safety_blocked",
        "commission_startup_anchor_load_failed",
    ):
        assert code in emitted, f"no commissioning builder raises {code}"
        assert code in mapped, f"the combined-test card has no copy for {code}"
        assert f"'{code}'" not in helper_js, (
            f"the /sound/ ladder now names {code} directly — move it into the "
            "loop above so its own sentence is pinned"
        )
    # The transport gates the preflight publishes both key household copy on
    # their ids (the closed-set guard below walks the builder for these).
    assert f"\n    {COMMISSIONING_TRANSPORT_GATE_ID}:" in helper_js, (
        "the /sound/ gate map does not name the transport gate"
    )
    assert "\n    commissioning_transport_armed:" in helper_js, (
        "the /sound/ gate map does not name the armed-transport gate"
    )
    # THE RETIRED RUNG, ABSENT FROM BOTH SURFACES.
    assert "commissioning_ring_transport_unsupported" not in helper_js, (
        "the /sound/ ladder still carries the retired ring-transport blocker"
    )
    assert "commissioning_ring_transport_unsupported" not in mapped, (
        "the combined-test card still maps the retired ring-transport blocker"
    )
    # And none of the copy may carry an operator command — the retired one's
    # `baseline-reemit`, or either new reconciler invocation.
    for command in ("baseline-reemit", "jasper-fanin-coupling-reconcile", "systemctl"):
        assert command not in helper_js, (
            f"a household surface carries the operator remedy verbatim: {command}"
        )
        assert not [
            message for _, message in _SUMMED_TEST_FAILURE_COPY if command in message
        ], f"the combined-test copy carries the operator remedy verbatim: {command}"


def test_active_speaker_setup_copy_has_no_backend_jargon():
    """The /sound/ active-speaker flow is a guided consumer setup, not an
    engineering console. User-facing copy must never leak backend vocabulary
    (CamillaDSP/YAML, "protected"/"safe path", rollout "slice", raw "evidence")
    and friendlySetupReason must never echo a raw snake_case code. See AGENTS.md
    "Web wizard conventions" and the active-crossover flow simplification."""
    js = _SOUND_MODULE.read_text()
    helper_js = _ACTIVE_SPEAKER_UI_MODULE.read_text()

    # No backend vocabulary in any user-visible string.
    for jargon in (
        "No YAML",
        "CamillaDSP baseline YAML",
        "A durable CamillaDSP baseline",
        "The baseline YAML is saved",
        "reloads CamillaDSP",
        "in this slice",
        "missing playback evidence",
        "the protected test setup",
        "the safe audio path",
        "safe test setup",
        "Polarity or delay issue",
        "was blocked before sound could play",
    ):
        assert jargon not in js, f"backend jargon leaked into main.js: {jargon!r}"

    # friendlySetupReason must collapse code-like strings to a calm sentence
    # instead of echoing the raw identifier (the old `text.replace(/_/g,' ')`).
    assert "return raw.replace(/_/g, ' ')" not in js
    assert "return outcome.replace(/_/g, ' ')" not in js
    assert "This driver can’t be tested yet — finish the earlier setup steps first." in helper_js

    # The new consumer copy is present and stable.
    assert "The working values below are ready to save and turn into a no-audio preview." in js
    assert (
        "Save the checked crossover as your active speaker profile. "
        "JTS validates and applies it in one step; no sound plays."
    ) in js
    assert "Your active speaker profile, built from the checked crossover and confirmed outputs." in js
    assert (
        "Your active speaker profile is saved. "
        "Finish applying it to start using it."
    ) in js
    assert "Sounds right" in js
    assert "Sounds hollow or thin" not in js
    assert "Needs adjustment" not in js
    assert "Too loud" not in js
    for confusing_copy in (
        "saved drivers",
        "saved crossover settings",
        "Save crossover settings",
        "Saved crossover settings",
        "saved driver info",
        "saved partial settings",
        "Builds the crossover plan from your saved settings",
    ):
        assert confusing_copy not in js

    # The pure vocabulary module owns the no-sound fallbacks and stays actionable.
    assert "Choose the driver again to try." in helper_js
    assert "Start the tone again so JTS can open the quiet driver test first." in helper_js
    assert "The tweeter guard still needs to be set up" in helper_js
    assert "did not complete" not in helper_js


def test_active_speaker_environment_payload_uses_configured_evidence_path(
    monkeypatch,
):
    calls = {}

    def fake_probe(**kwargs):
        calls.update(kwargs)
        return {
            "status": "blocked",
            "load_gate": "path_safety_evidence_missing",
            "blocker_count": 2,
            "safe_playback": {"playback_allowed": False},
        }

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        "/tmp/path-safety.json",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.environment.probe_active_speaker_environment",
        fake_probe,
    )

    payload = sound_setup._active_speaker_environment_payload()

    assert payload["status"] == "blocked"
    assert calls == {
        "path_safety_evidence_path": "/tmp/path-safety.json",
    }


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
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    monkeypatch.setenv("JASPER_AUDIO_LAB_TONE_BACKEND", "wav_artifact")
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_environment_payload",
        lambda: {
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
        },
    )

    from jasper.active_speaker.safe_playback import arm_safe_playback_session

    armed = arm_safe_playback_session(
        sound_setup._active_speaker_environment_payload()
    )
    guarded = sound_setup._active_speaker_calibration_level_payload({
        "action": "set",
        "level_dbfs": -55,
    })
    status = sound_setup._active_speaker_safe_playback_payload()
    stopped = sound_setup._active_speaker_stop_payload()
    stopped_level = sound_setup._active_speaker_calibration_level_payload()

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
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_environment_payload",
        lambda: {
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
        },
    )

    def fail_reset(*args, **kwargs):
        raise OSError("state path is unavailable")

    from jasper.active_speaker.safe_playback import arm_safe_playback_session

    arm_safe_playback_session(sound_setup._active_speaker_environment_payload())
    monkeypatch.setattr(level_mod, "update_calibration_level_state", fail_reset)

    stopped = sound_setup._active_speaker_stop_payload()

    assert stopped["status"] == "stopped"
    assert stopped["playback"]["status"] == "stopped"
    assert stopped["calibration_level"]["status"] == "reset_failed"


def _active_speaker_mono_topology_payload(
    *,
    protection_status: str,
    card_id: str | None = "DAC8",
    identity_verified: bool = False,
) -> dict:
    woofer: dict = {"role": "woofer", "physical_output_index": 0}
    tweeter: dict = {
        "role": "tweeter",
        "physical_output_index": 1,
        "startup_muted": True,
        "protection_required": True,
        "protection_status": protection_status,
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


def _confirm_channel_identity(group_id: str, *roles: str) -> None:
    """Record the per-lane audition a save payload can no longer claim."""

    with output_topology_mutation() as mutation:
        topology = mutation.snapshot().topology
        for role in roles:
            topology = set_channel_identity_verified(
                topology,
                speaker_group_id=group_id,
                role=role,
                identity_verified=True,
            )
        mutation.save(topology)


_ACTIVE_SPEAKER_STATE_FILENAMES = {
    "JASPER_OUTPUT_TOPOLOGY_PATH": "output_topology.json",
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design_draft.json",
    "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE": "crossover_preview.json",
    "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE": "measurements.json",
    "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR": "tone-artifacts",
    "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE": "safe-playback.json",
    "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH": "active_staged.yml",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "active_staged.json",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path_safety.json",
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
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
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
                        "protection_status": "present",
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
    """THE FLIP, at the surface the owner hit: /sound/setup/ refused these
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

    route = saved["active_playback_route"]
    assert route["playback_device_source"] == OUTPUTD_ACTIVE_LANE_SOURCE
    assert route["transport_channel_count"] == 2
    assert route["issues"] == []
    assert route["ready"] is True


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
    grouping_env.write_text("JASPER_OUTPUTD_DAC_CONTENT_FIFO=/run/armed.fifo\n")
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
            grouping_env.write_text("JASPER_OUTPUTD_DAC_CONTENT_FIFO=\n")
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
    assert grouping_env.read_text() == "JASPER_OUTPUTD_DAC_CONTENT_FIFO=\n"
    assert saved["reconcile"] is sentinel
    assert set(saved["hardware_adoption"]) == {"allowed", "identity"}


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
    ("reconcile", "verdict"),
    [
        (
            RECONCILE_FAILED,
            {
                "status": "needs_attention",
                "message": (
                    "Speaker layout was saved, but audio remains off. "
                    "Open Status before continuing."
                ),
            },
        ),
        (
            RECONCILE_STILL_CONVERGING,
            {
                "status": "converging",
                "message": (
                    "Speaker layout was saved and is still applying. "
                    "Check Status in a moment."
                ),
            },
        ),
    ],
)
def test_topology_save_reports_the_reconcile_verdict_without_leaking_it(
    monkeypatch, tmp_path: Path, reconcile, verdict,
):
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "topology.json"))
    _stub_audio_stops(monkeypatch)
    _stub_reconcile(monkeypatch, reconcile)

    saved = sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )

    assert saved["save"] == verdict
    assert "private backend detail" not in saved["save"]["message"]


def test_topology_save_parks_before_replacing_saved_layout(
    monkeypatch, tmp_path: Path,
):
    from jasper.output_topology import new_topology_draft, save_output_topology

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


def test_topology_save_does_not_restore_old_graph_for_a_post_write_read_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jasper import output_topology as topology_mod
    from jasper.output_topology import new_topology_draft, save_output_topology

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
        sound_setup,
        "_output_topology_payload",
        lambda: {"output_topology": {"status": "verified"}},
    )
    _stub_audio_stops(monkeypatch)

    sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )

    assert snapshot_reads == 1
    assert events == ["park", "converge-new-graph"]
    assert load_output_topology(path).name != "Old layout"


def test_topology_save_refuses_invalid_input_before_stopping_or_parking(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "topology.json"))
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_summed_test_tone",
        lambda **_kwargs: pytest.fail("invalid input must not stop audio"),
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda **_kwargs: pytest.fail("invalid input must not stop audio"),
    )
    monkeypatch.setattr(
        sound_setup,
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
            if events == [
                "summed:output_topology_save",
                "commission:output_topology_save",
                "safe",
            ]
            else pytest.fail(f"expected all active audio sessions stopped first: {events}")
        ),
    )

    sound_setup._save_output_topology_payload(_innomaker_topology_payload(active=False))


def test_refused_layout_reaches_the_page_as_a_rendered_error(
    monkeypatch,
    tmp_path: Path,
):
    """Pin the wire shape the page already knows how to render.

    ``deploy/assets/sound-profile/js/main.js``'s ``saveOutputTopology`` does
    ``if (!resp.ok) throw new Error(payload.error || ...)`` and surfaces the
    message while keeping the operator's draft on screen — so the refusal has
    to arrive as a non-2xx with an ``error`` string, and must NOT take the 409
    branch (that one re-ingests the server's topology and would wipe the draft).
    """
    register_passive_only_dac(monkeypatch)
    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))
    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(
            base,
            "/output-topology",
            {
                "output_topology": _no_lane_topology_payload(active=True),
                # The page always echoes the revision it last read; "missing"
                # is what the endpoint reports before anything is saved.
                "topology_revision": "missing",
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
    from jasper.output_topology import load_output_topology

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


def test_sub_crossover_bounds_match_python():
    """The JS active-speaker-ui bass-management bounds MUST equal the Python
    profile + output_topology constants — drift would let the UI offer a corner
    the server fail-loud rejects (or clamp where the server blocks)."""
    from jasper.active_speaker.profile import (
        DEFAULT_SUB_CROSSOVER_HZ,
        SUB_CROSSOVER_HZ_HI,
        SUB_CROSSOVER_HZ_LO,
    )

    js = _ACTIVE_SPEAKER_UI_JS.read_text()
    assert f"DEFAULT_SUB_CROSSOVER_HZ = {DEFAULT_SUB_CROSSOVER_HZ}" in js
    assert f"SUB_CROSSOVER_HZ_LO = {SUB_CROSSOVER_HZ_LO}" in js
    assert f"SUB_CROSSOVER_HZ_HI = {SUB_CROSSOVER_HZ_HI}" in js


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
        "expected_revision": 0,
        "operator_inputs": {
            "woofer": "Dayton Epique E150HE-44",
            "tweeter": "Eminence F110M-8",
        },
        "driver_research": _active_speaker_driver_research_payload(
            frequency_hz=frequency_hz,
        ),
    })
    return sound_setup._active_speaker_crossover_preview_save_payload()


def test_driver_research_request_payload_is_target_bound_and_silent(
    monkeypatch,
) -> None:
    from tests.active_speaker_fixtures import mono_output_topology

    topology = mono_output_topology(card_id=None)
    monkeypatch.setattr(sound_setup, "load_output_topology", lambda: topology)

    payload = sound_setup._active_speaker_driver_research_request_payload({
        "operator_inputs": {
            "woofer": "Example W6",
            "tweeter": "Example T1",
            "notes": "sealed cabinet",
        }
    })

    request = payload["request"]
    assert request["targets"][0]["target_id"] == "mono:woofer"
    assert request["targets"][1]["target_id"] == "mono:tweeter"
    assert request["request_fingerprint"] in payload["prompt"]
    assert payload["safety"] == {
        "no_audio": True,
        "loads_camilla": False,
        "applies_filters": False,
        "authorizes_playback": False,
        "research_is_advisory": True,
    }
    with pytest.raises(ValueError, match="unknown fields: typo"):
        sound_setup._active_speaker_driver_research_request_payload({
            "operator_inputs": {},
            "typo": "must not disappear silently",
        })


def test_active_speaker_crossover_preview_refreshes_current_output_topology(
    monkeypatch,
    tmp_path: Path,
):
    _set_active_speaker_state_paths(monkeypatch, tmp_path)

    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="required_missing")
    )
    _confirm_channel_identity("mono", "woofer", "tweeter")
    refreshed = _save_active_speaker_design_and_preview()

    assert refreshed["status"] == "ready_for_protected_staging"
    assert "tweeter_protection_unverified" not in {
        issue["code"] for issue in refreshed["issues"]
    }
    filters = refreshed["groups"][0]["crossovers"][0]["filters"]
    tweeter_filter = next(
        item for item in filters
        if item["role"] == "tweeter"
    )
    assert tweeter_filter["channel"]["identity_verified"] is True
    assert tweeter_filter["channel"]["protection_status"] == "software_guard_requested"


def test_active_speaker_summed_test_records_current_artifact(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.active_speaker.measurement import record_driver_measurement
    from jasper.active_speaker.safe_playback import arm_safe_playback_session
    from jasper.output_topology import load_output_topology

    _set_active_speaker_state_paths(
        monkeypatch,
        tmp_path,
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
    )
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="present")
    )
    _confirm_channel_identity("mono", "woofer", "tweeter")
    _save_active_speaker_design_and_preview()
    arm_safe_playback_session({
        "status": "pass",
        "load_gate": "ready",
        "ok_to_load_active_config": True,
        "camilla_config": {"classification": "active_startup_candidate"},
        "safe_playback": {"playback_allowed": False},
        "issues": [],
    })
    topology = load_output_topology()
    for role, output_index in (("woofer", 0), ("tweeter", 1)):
        playback_id = f"playback-{role}"
        target = {
            "speaker_group_id": "mono",
            "role": role,
            "driver_role": role,
            "output_index": output_index,
        }
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42,
                "playback_id": playback_id,
            },
            safe_session={
                "status": "armed",
                "quiet_start": {
                    "status": "floor_confirmed",
                    "floor_audio_confirmed": True,
                    "last_operator_result": {
                        "accepted": True,
                        "outcome": "heard_correct_driver",
                        "playback_id": playback_id,
                        "target": target,
                    },
                },
            },
        )

    payload = asyncio.run(sound_setup._active_speaker_summed_test_payload(
        {
            "speaker_group_id": "mono",
            "audio": False,
        },
        camilla_factory=lambda: None,
    ))
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]

    assert payload["playback"]["status"] == "completed"
    assert payload["playback"]["audio_emitted"] is False
    assert payload["playback"]["artifact"]["target_output_indices"] == [0, 1]
    assert latest["captured"] is True
    assert latest["audio_emitted"] is False
    assert latest["target_output_indices"] == [0, 1]


def test_active_speaker_protection_and_stage_config_payloads_are_no_load(
    monkeypatch,
    tmp_path: Path,
):
    _set_active_speaker_state_paths(
        monkeypatch,
        tmp_path,
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH",
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE", "hw:DAC8,0")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="required_missing")
    )
    _confirm_channel_identity("mono", "woofer", "tweeter")
    saved = sound_setup._output_topology_payload()

    preview_ready = _save_active_speaker_design_and_preview()
    staged = sound_setup._active_speaker_stage_config_payload({})
    loaded = sound_setup._active_speaker_staged_config_payload()

    assert saved["output_topology"]["status"] == "verified"
    assert (
        saved["output_topology"]["speaker_groups"][0]["channels"][1][
            "protection_status"
        ]
        == "software_guard_requested"
    )
    assert preview_ready["status"] == "ready_for_protected_staging"
    assert staged["status"] == "staged"
    assert staged["preset"]["source"]["mode"] == "crossover_preview"
    assert staged["config"]["basename"] == "active_staged.yml"
    assert staged["config"]["playback_device"] == "hw:DAC8,0"
    assert staged["config"]["tweeter_protective_highpass_hz"] == 5000
    assert staged["load"]["load_allowed"] is False
    assert Path(staged["config"]["path"]).exists()
    assert loaded["status"] == "staged"


def test_active_speaker_path_safety_payload_writes_no_audio_evidence(
    monkeypatch,
    tmp_path: Path,
):
    _set_active_speaker_state_paths(
        monkeypatch,
        tmp_path,
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH",
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE", "hw:DAC8,0")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="software_guard_requested",
        )
    )
    _confirm_channel_identity("mono", "woofer", "tweeter")
    preview = _save_active_speaker_design_and_preview()
    staged = sound_setup._active_speaker_stage_config_payload({})
    fake = FakeCamilla(staged["config"]["path"])

    assert preview["status"] == "ready_for_protected_staging"
    assert staged["status"] == "staged"
    assert staged["preset"]["source"]["mode"] == "crossover_preview"
    payload = asyncio.run(
        sound_setup._active_speaker_check_path_safety_payload(
            camilla_factory=lambda: fake,
        )
    )

    evidence_path = Path(payload["evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["report"]["ok_to_load_active_config"] is True
    assert payload["startup_load"]["preflight"]["path_safety"]["load_gate"] == "ready"
    assert evidence["evidence_mode"] == "startup_load_preflight"
    assert fake.set_calls == []


def test_active_speaker_stage_config_rejects_non_string_playback_device() -> None:
    with pytest.raises(ValueError, match="playback_device must be a string"):
        sound_setup._active_speaker_stage_config_payload({
            "playback_device": {"device": "hw:DAC8,0"},
        })


def test_active_speaker_stage_config_route_requires_current_preview(
    monkeypatch,
    tmp_path: Path,
):
    _set_active_speaker_state_paths(
        monkeypatch,
        tmp_path,
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH",
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE", "hw:DAC8,0")
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(protection_status="present")
    )

    payload = sound_setup._active_speaker_stage_config_payload({})

    assert payload["status"] == "blocked"
    assert payload["config"]["exists"] is False
    assert payload["preset"]["source"]["mode"] == "crossover_preview"
    assert "crossover_preview_not_ready" in {
        issue["code"] for issue in payload["issues"]
    }


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


def test_sound_output_topology_payload_is_no_audio_draft(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    _record_dac8x()

    envelope = sound_setup._output_topology_payload()
    payload = envelope["output_topology"]

    assert payload["kind"] == OUTPUT_TOPOLOGY_KIND
    assert payload["status"] == "draft"
    assert payload["hardware"]["physical_output_count"] == 8
    assert envelope["clock_domain"]["status"] == "single_device_clock"
    assert envelope["clock_domain"]["multi_device_aggregate_supported"] is False
    assert payload["safety"]["sound_tests_allowed"] is False
    assert payload["evaluation"]["warnings"][0]["code"] == "no_speaker_groups"


def test_output_topology_payload_does_not_take_mutation_lock(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.setattr(
        sound_setup,
        "output_topology_mutation",
        lambda: pytest.fail("read-only payload must not take the mutation lock"),
    )

    envelope = sound_setup._output_topology_payload()

    assert envelope["topology_revision"] == "missing"


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
    assert envelope["active_playback_route"]["kind"] == (
        "jts_active_speaker_playback_route_capability"
    )
    assert envelope["active_playback_route"]["playback_device_source"] == (
        "outputd_active_lane"
    )
    assert envelope["active_playback_route"]["transport_channel_count"] == 2
    # The reset precondition is derived from, never a copy of, detection.
    assert set(envelope["hardware_adoption"]) == {"allowed", "identity"}
    # #2812 S5: the JS mismatch card (and #2819's re-pin offer nested inside
    # it) is a pure proxy for this key now — it does not recompute the rule
    # itself. Deleting the key here would silently kill both with every test
    # elsewhere still green (the JS harness fixture supplies its own value
    # independent of this payload builder), so the key's presence is pinned at
    # its one source.
    assert "hardware_mismatch" in envelope


def test_active_speaker_design_draft_route_persists_saved_topology_research(
    monkeypatch,
    tmp_path: Path,
):
    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)
    draft_path = paths["JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"]
    preview_path = paths["JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE"]
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="software_guard_requested",
            card_id=None,
            identity_verified=True,
        )
    )

    payload = sound_setup._active_speaker_design_draft_save_payload({
        "expected_revision": 0,
        "operator_inputs": {
            "woofer": "Dayton Epique E150HE-44",
            "tweeter": "Eminence F110M-8",
        },
        "driver_research": {
            "artifact_schema_version": 1,
            "kind": "jts_active_crossover_driver_research",
            "drivers": [
                {
                    "role": "woofer",
                    "model": "Epique E150HE-44",
                    "recommended_lowpass_hz": 2500,
                    "sources": ["https://example.test/woofer"],
                },
                {
                    "role": "tweeter",
                    "model": "F110M-8",
                    "recommended_highpass_hz": 2500,
                    "do_not_test_below_hz": 1200,
                    "sources": ["https://example.test/tweeter"],
                },
            ],
            "crossover_candidates": [
                {
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2500,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                }
            ],
        },
    })
    loaded = sound_setup._active_speaker_design_draft_payload()

    assert payload["kind"] == "jts_active_speaker_design_draft"
    assert payload["status"] == "ready_for_review"
    assert payload["summary"]["driver_count"] == 2
    assert payload["summary"]["crossover_candidate_count"] == 1
    assert payload["safety"]["no_audio"] is True
    assert loaded["status"] == "ready_for_review"
    assert json.loads(draft_path.read_text(encoding="utf-8"))["status"] == (
        "ready_for_review"
    )

    preview = sound_setup._active_speaker_crossover_preview_save_payload()
    loaded_preview = sound_setup._active_speaker_crossover_preview_payload()

    assert preview["kind"] == "jts_active_speaker_crossover_preview"
    assert preview["status"] == "ready_for_protected_staging"
    assert preview["safety"]["no_audio"] is True
    assert preview["permissions"]["may_not_emit_camilla_yaml"] is True
    assert loaded_preview["status"] == "ready_for_protected_staging"
    assert json.loads(preview_path.read_text(encoding="utf-8"))["status"] == (
        "ready_for_protected_staging"
    )

    stale_draft = json.loads(draft_path.read_text(encoding="utf-8"))
    stale_draft["driver_research"]["crossover_candidates"][0]["frequency_hz"] = 3200
    stale_draft["updated_at"] = "2026-06-10T12:45:00Z"
    draft_path.write_text(json.dumps(stale_draft), encoding="utf-8")

    stale_preview = sound_setup._active_speaker_crossover_preview_payload()

    assert stale_preview["status"] == "stale"
    assert stale_preview["permissions"]["may_prepare_protected_startup_config"] is False
    assert "crossover_preview_stale_design_draft" in {
        issue["code"] for issue in stale_preview["issues"]
    }


def test_design_draft_save_payload_requires_strict_revision_contract() -> None:
    with pytest.raises(ValueError, match="requires expected_revision"):
        sound_setup._active_speaker_design_draft_save_payload({})
    with pytest.raises(ValueError, match="expected_revision must be"):
        sound_setup._active_speaker_design_draft_save_payload({
            "expected_revision": True,
        })
    # The separate confirm step was retired -- saving the declaration IS
    # declaring it -- so this key is no longer part of the request contract at
    # all, and a stale client sending it is told so rather than silently obeyed.
    with pytest.raises(ValueError, match="unknown fields: confirm_safety_profile"):
        sound_setup._active_speaker_design_draft_save_payload({
            "expected_revision": 0,
            "confirm_safety_profile": True,
        })
    with pytest.raises(ValueError, match="unknown fields: typo"):
        sound_setup._active_speaker_design_draft_save_payload({
            "expected_revision": 0,
            "typo": "ignored before this contract",
        })


def test_design_draft_http_conflict_returns_fresh_revision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jasper.output_topology import output_topology_mutation
    from tests.active_speaker_fixtures import mono_output_topology

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft.json"),
    )
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    with output_topology_mutation(topology_path) as mutation:
        mutation.save(mono_output_topology(card_id=None))
    with sound_server(tmp_path) as base:
        first = json_post_with_csrf(
            base,
            "/active-speaker/design-draft",
            {"expected_revision": 0, "operator_inputs": {"notes": "first"}},
        )
        assert json.loads(first.read().decode("utf-8"))["revision"] == 1

        with output_topology_mutation(topology_path) as mutation:
            changed = mutation.snapshot().topology.to_dict()
            changed["speaker_groups"][0]["channels"][1][
                "driver_style"
            ] = "ribbon_tweeter"
            mutation.save(OutputTopology.from_mapping(changed))

        conflict = json_post_with_csrf(
            base,
            "/active-speaker/design-draft",
            {"expected_revision": 0, "operator_inputs": {"notes": "stale"}},
            expect_status=409,
        )
        fresh = json.loads(conflict.read().decode("utf-8"))

    assert fresh["revision"] == 1
    assert fresh["operator_inputs"]["notes"] == "first"
    assert fresh["driver_safety_profile_evaluation"]["status"] == "stale"
    assert "another session" in fresh["error"]


def test_preview_preserves_bound_v2_confirmation_and_does_not_rewrite_draft(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jasper.active_speaker.driver_safety import build_driver_research_request
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.test_active_speaker_driver_safety import (
        _manual_settings,
        _operator_inputs,
        _research_result,
    )

    topology = mono_output_topology(card_id=None)
    paths = _set_active_speaker_state_paths(monkeypatch, tmp_path)
    draft_path = paths["JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"]
    from jasper.output_topology import save_output_topology

    save_output_topology(topology)
    monkeypatch.setattr(sound_setup, "load_output_topology", lambda: topology)
    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
    saved = sound_setup._active_speaker_design_draft_save_payload({
        "expected_revision": 0,
        "driver_research_request": request,
        "driver_research": _research_result(request),
        "manual_settings": _manual_settings(),
        "operator_inputs": _operator_inputs(),
    })
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["updated_at"] = "2026-08-14T16:33:48Z"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    before = draft_path.read_bytes()

    preview = sound_setup._active_speaker_crossover_preview_save_payload()

    after = draft_path.read_bytes()
    loaded = sound_setup._active_speaker_design_draft_payload()
    assert after == before
    assert loaded["revision"] == saved["revision"] == 1
    assert loaded["driver_research_request"] == saved["driver_research_request"]
    assert loaded["driver_research"] == saved["driver_research"]
    assert loaded["driver_safety_profile"]["confirmation"] == (
        saved["driver_safety_profile"]["confirmation"]
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
    from jasper.output_topology import save_output_topology

    save_output_topology(topology)
    monkeypatch.setattr(sound_setup, "load_output_topology", lambda: topology)
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
        "expected_revision": 0,
        "manual_settings": manual,
        "operator_inputs": operator_inputs or {},
    })
    assert fsync_calls == []  # ordinary wizard save: no fsync
    assert saved["driver_safety_profile_evaluation"]["confirmed_and_current"] is True
    return fsync_calls


def _geometry(fc_hz: float, slope_db_per_octave: int):
    from jasper.active_speaker.crossover_declaration import CrossoverGeometry

    return CrossoverGeometry(
        fc_hz=fc_hz,
        filter_type="Linkwitz-Riley",
        slope_db_per_octave=slope_db_per_octave,
    )


def test_measured_fc_uses_sound_cas_and_leaves_the_loop_open(
    monkeypatch, tmp_path: Path,
) -> None:
    """The nanny loop, pinned shut at the seam that caused it.

    Accepting a measured crossover writes the Sound declaration from the ROOT
    correction-web process, with no human present. Before this change that write
    went through ``build_driver_safety_profile`` with ``confirm=False``, so it
    could only ever CARRY a confirmation forward -- and on a box where there was
    nothing to carry (a prior profile already stale/malformed, or a driver
    detail edited between measurements) the machine's own write landed
    ``needs_confirmation`` and every measurement surface then refused until a
    human clicked Confirm in the wizard. The declaration the machine just wrote
    must read as current.
    """
    from jasper.active_speaker.design_draft import load_design_draft

    _declared_candidate_box(
        monkeypatch, tmp_path, operator_inputs={"notes": "keep this"}
    )

    saved = sound_setup.apply_measured_crossover_geometry(
        expected_revision=1,
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
    assert profile["status"] == "confirmed"
    assert (
        profile["confirmation"]["confirmed_fingerprint"]
        == profile["profile_fingerprint"]
    )
    assert saved["driver_safety_profile_evaluation"]["confirmed_and_current"] is True
    reloaded = load_design_draft(topology=sound_setup.load_output_topology())
    assert reloaded["driver_safety_profile_evaluation"]["confirmed_and_current"] is True
    # And it is still not an audio authorization.
    assert profile["authorizes_playback"] is False

    with pytest.raises(ValueError, match="another session"):
        sound_setup.apply_measured_crossover_geometry(
            expected_revision=1,
            between_roles=("woofer", "tweeter"),
            configured=_geometry(5750, 24),
            selected=_geometry(6000, 24),
        )
    assert load_design_draft()["revision"] == 2


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
        expected_revision=1,
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
            expected_revision=1,
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
            "protection_status": "present",
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
    _confirm_channel_identity("left", "woofer", "tweeter")
    _confirm_channel_identity("right", "woofer", "tweeter")
    payload = sound_setup._output_topology_payload()

    topology = payload["output_topology"]

    assert topology["status"] == "verified"
    assert topology["hardware"]["physical_output_count"] == 4
    assert payload["clock_domain"]["status"] == "dual_apple_composite_clock"
    assert payload["clock_domain"]["composite_clock_supported"] is True
    assert payload["clock_domain"]["multi_device_aggregate_supported"] is False
    assert payload["channel_identity"]["verified_channel_count"] == 4
    assert topology["safety"]["sound_tests_allowed"] is False


def test_sound_output_topology_save_discloses_a_cross_child_speaker_group(
    monkeypatch,
    tmp_path: Path,
):
    """POST /output-topology accepts a cross-child layout and says so.

    Issue #2486. One cabinet with its woofer on dongle A and its tweeter on
    dongle B puts an uncorrected clock seam inside a crossover. That is a
    fidelity cost, not a hearing-safety one, so per the never-nanny ruling the
    save is ACCEPTED and the verdict rides back in the response for the wizard
    to show. This pins both halves: not refused, and not silent.
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
                        "protection_status": "present",
                    },
                ],
            },
        ],
        "routing": {"mono_group_id": "mono"},
    })
    _confirm_channel_identity("mono", "woofer", "tweeter")

    topology = sound_setup._output_topology_payload()["output_topology"]
    verdicts = [
        issue for issue in topology["evaluation"]["warnings"]
        if issue["code"] == CROSS_CHILD_GROUP_CODE
    ]

    assert len(verdicts) == 1
    assert verdicts[0]["group_id"] == "mono"
    assert verdicts[0]["child_ids"] == ["left_dac", "right_dac"]
    # Accepted, not refused: it persisted and it is not blocked.
    assert path.exists()
    assert topology["status"] == "verified"
    assert topology["safety"]["blockers"] == []


def test_active_speaker_tone_backend_status_is_explicit_lab_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH",
        str(tmp_path / "output_topology.json"),
    )
    monkeypatch.delenv("JASPER_AUDIO_LAB_TONE_BACKEND", raising=False)
    monkeypatch.delenv("JASPER_AUDIO_LAB_TEST_PCM", raising=False)
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="software_guard_requested",
            card_id="sndrpihifiberry",
            identity_verified=True,
        )
    )

    status = sound_setup._active_speaker_tone_backend_status()

    assert status["status"] == "artifact_only"
    assert status["backend"] == "wav_artifact"
    assert status["test_pcm"] is None
    assert status["playback_device"] is None
    assert status["default_pcm_source"] == "explicit_lab_pcm"
    assert status["channel_count"] == 8
    assert status["requires_protected_startup"] is True

    monkeypatch.setenv("JASPER_AUDIO_LAB_TONE_BACKEND", "direct_dac")
    stale_env_status = sound_setup._active_speaker_tone_backend_status()
    assert stale_env_status["status"] == "blocked"
    assert stale_env_status["backend"] == "direct_dac"
    assert stale_env_status["audio_enabled"] is False
    assert "unknown_tone_backend" in {
        issue["code"] for issue in stale_env_status["issues"]
    }

    # Selecting the audio-lab backend echoes the operator's typed values but
    # is BLOCKED, not audio-enabled: nothing wires an audio backend, so the
    # tone renders to a WAV artifact whatever the knob says.
    monkeypatch.setenv("JASPER_AUDIO_LAB_TONE_BACKEND", "aplay")
    monkeypatch.setenv("JASPER_AUDIO_LAB_TEST_PCM", "hw:Active")
    lab_status = sound_setup._active_speaker_tone_backend_status()
    assert lab_status["status"] == "blocked"
    assert lab_status["audio_enabled"] is False
    assert lab_status["backend"] == "aplay"
    assert lab_status["audio_backend"] is None
    assert "tone_backend_not_wired" in {
        issue["code"] for issue in lab_status["issues"]
    }
    assert lab_status["playback_device"] == "hw:Active"
    assert lab_status["channel_count"] == 8
    assert lab_status["requires_protected_startup"] is True


def test_sound_output_topology_save_validates_and_persists_complete_contract(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(
        {"output_topology": _passive_left_topology_payload()}
    )
    _confirm_channel_identity("left", "full_range")
    payload = sound_setup._output_topology_payload()
    topology = payload["output_topology"]
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert topology["status"] == "verified"
    assert topology["evaluation"]["assigned_output_count"] == 1
    assert topology["safety"]["sound_tests_allowed"] is False
    assert saved["status"] == "verified"
    assert saved["speaker_groups"][0]["channels"][0]["human_output_label"] == (
        "DAC output 1"
    )
    assert payload["channel_identity"]["verified_channel_count"] == 1
    assert payload["clock_domain"]["status"] == "single_device_clock"
    assert payload["topology_revision"].startswith("sha256:")


def test_sound_output_topology_save_rejects_stale_revision(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.output_topology import new_topology_draft, save_output_topology

    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    stale = sound_setup._output_topology_payload()
    active_topology = _active_speaker_mono_topology_payload(
        protection_status="software_guard_requested"
    )

    save_output_topology(new_topology_draft(), path=path)

    with pytest.raises(sound_setup.OutputTopologyRevisionConflict):
        sound_setup._save_output_topology_payload(
            {
                "output_topology": active_topology,
                "topology_revision": stale["topology_revision"],
            },
            require_revision=True,
        )

    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["speaker_groups"] == []


def test_sound_output_topology_save_serializes_concurrent_writers(
    monkeypatch,
    tmp_path: Path,
):
    """Two writers racing the same revision: exactly one wins, no lost update.

    ``_save_output_topology_payload`` runs under ``ThreadingHTTPServer`` (one
    thread per request), so the revision-compare and the file write must be a
    single critical section. This test parks writer A *inside*
    ``save_output_topology`` (after it has read+validated the revision but
    before it publishes the new file) while writer B begins. Without the lock,
    B reads the still-original revision, passes the stale-check, and clobbers
    A's write (the exact lost-update the revision guard exists to prevent).
    With the lock, B blocks until A fully publishes, then observes the new
    revision and raises ``OutputTopologyRevisionConflict``.
    """

    from jasper.output_topology import (
        OutputTopologyMutation,
        new_topology_draft,
        save_output_topology,
    )

    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))

    # Seed a known starting topology and capture the revision both writers see.
    save_output_topology(new_topology_draft(), path=path)
    start_revision = sound_setup._output_topology_payload()["topology_revision"]

    topology_a = _active_speaker_mono_topology_payload(
        protection_status="software_guard_requested",
        card_id="DAC_A",
    )
    topology_b = _active_speaker_mono_topology_payload(
        protection_status="software_guard_requested",
        card_id="DAC_B",
    )

    real_save = OutputTopologyMutation.save
    a_in_critical_section = threading.Event()
    release_a = threading.Event()

    def parking_save(self, topology):
        # Only the first writer to reach the write parks; the parked writer is
        # holding the critical section (it has already passed the revision
        # compare). Releasing it after B has had its chance to run exercises
        # the interleaving the lock must prevent.
        if not a_in_critical_section.is_set():
            a_in_critical_section.set()
            assert release_a.wait(timeout=5.0), "writer A was never released"
        return real_save(self, topology)

    monkeypatch.setattr(OutputTopologyMutation, "save", parking_save)

    results: dict[str, object] = {}

    def writer(name: str, topology: dict) -> None:
        try:
            sound_setup._save_output_topology_payload(
                {
                    "output_topology": topology,
                    "topology_revision": start_revision,
                },
                require_revision=True,
            )
            results[name] = "saved"
        except sound_setup.OutputTopologyRevisionConflict:
            results[name] = "conflict"
        except BaseException as exc:  # noqa: BLE001 - surface unexpected failures
            results[name] = exc

    thread_a = threading.Thread(target=writer, args=("A", topology_a))
    thread_a.start()
    assert a_in_critical_section.wait(timeout=5.0), "writer A never started saving"

    # A is parked inside the write holding the lock. Start B; with the lock it
    # must block on acquisition, so it cannot finish before we release A.
    thread_b = threading.Thread(target=writer, args=("B", topology_b))
    thread_b.start()
    thread_b.join(timeout=0.5)
    assert thread_b.is_alive(), (
        "writer B completed while writer A held the critical section — the "
        "compare-and-write is not serialized (TOCTOU)"
    )

    release_a.set()
    thread_a.join(timeout=5.0)
    thread_b.join(timeout=5.0)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()

    # Exactly one writer wins; the other sees the advanced revision and is
    # rejected. No lost update: the persisted card_id is the winner's.
    outcomes = sorted(
        v for v in results.values() if isinstance(v, str)
    )
    unexpected = [v for v in results.values() if not isinstance(v, str)]
    assert not unexpected, f"writer raised unexpectedly: {unexpected}"
    assert outcomes == ["conflict", "saved"], results

    winner = next(name for name, outcome in results.items() if outcome == "saved")
    expected_card = {"A": "DAC_A", "B": "DAC_B"}[winner]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["hardware"]["card_id"] == expected_card


def test_sound_channel_identity_route_marks_saved_topology_only(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(_passive_left_topology_payload())

    payload = sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left",
        "role": "full_range",
        "identity_verified": True,
    })
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert payload["channel_identity"]["status"] == "verified"
    assert payload["channel_identity"]["verified_channel_count"] == 1
    assert payload["clock_domain"]["multi_device_aggregate_supported"] is False
    assert payload["output_topology"]["status"] == "verified"
    assert payload["topology_revision"].startswith("sha256:")
    assert payload["hardware_adoption"]["identity"].startswith("sha256:")
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is True

    payload = sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left",
        "role": "full_range",
        "identity_verified": False,
    })
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert payload["channel_identity"]["status"] == "needs_verification"
    assert payload["channel_identity"]["verified_channel_count"] == 0
    assert payload["output_topology"]["status"] == "valid"
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is False


_PENDING_WOOFER_STEP = {
    "role": "woofer",
    "playback_id": "pb-woofer",
    "gain_db": -80.0,
}


def _stub_confirmed_ramp_ack(monkeypatch) -> None:
    """A pending woofer step whose operator ack comes back confirmed."""

    async def fake_ack(*, outcome, load_config):
        assert outcome == "heard_correct_driver"
        assert load_config is not None
        return {
            "status": "confirmed",
            "acknowledged_step": dict(_PENDING_WOOFER_STEP),
            "issues": [],
        }

    monkeypatch.setattr(
        "jasper.active_speaker.commission_ramp.load_ramp_state",
        lambda: {
            "speaker_group_id": "main",
            "pending": dict(_PENDING_WOOFER_STEP),
        },
    )
    monkeypatch.setattr(
        "jasper.active_speaker.commission_ramp.record_ramp_operator_ack", fake_ack
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda *, reason: {"status": "stopped", "reason": reason},
    )
    monkeypatch.setattr(
        sound_setup, "commission_seams", lambda _cam: (object(), None, None)
    )


async def _ack_confirming_output_identity():
    return await sound_setup._active_speaker_commission_ramp_ack_payload(
        {"outcome": "heard_correct_driver", "confirm_output_identity": True},
        camilla_factory=lambda: object(),
    )


async def test_commission_ack_can_promote_output_identity_before_driver_proof(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(_bench_active_topology_payload())
    _stub_confirmed_ramp_ack(monkeypatch)
    recorded: dict[str, object] = {}

    def fake_record_driver_measurement(topology, raw, **_kwargs):
        recorded["raw"] = dict(raw)
        recorded["woofer_identity"] = (
            topology.speaker_groups[0].channels[0].identity_verified
        )
        return {
            "status": "needs_summed_validation",
            "summary": {
                "driver_checks_complete": False,
                "captured_driver_check_count": 1,
            },
        }

    monkeypatch.setattr(
        "jasper.active_speaker.measurement.record_driver_measurement",
        fake_record_driver_measurement,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.calibration_level.load_calibration_level_state",
        lambda: {},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.safe_playback.load_safe_playback_state", lambda: {}
    )

    payload = await _ack_confirming_output_identity()
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert payload["status"] == "confirmed"
    assert payload["channel_identity"]["verified_channel_count"] == 1
    assert payload["output_topology"]["speaker_groups"][0]["channels"][0][
        "identity_verified"
    ] is True
    assert payload["topology_revision"].startswith("sha256:")
    assert payload["hardware_adoption"]["identity"].startswith("sha256:")
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is True
    # The recorder saw the promoted lane, so the promotion precedes the proof.
    assert recorded["woofer_identity"] is True
    assert recorded["raw"]["role"] == "woofer"
    assert recorded["raw"]["playback_id"] == "pb-woofer"


async def test_commission_ack_fails_when_output_identity_cannot_save(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.output_topology import OutputTopologyMutation

    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(_bench_active_topology_payload())
    _stub_confirmed_ramp_ack(monkeypatch)

    def fail_save(_mutation, _topology):
        raise OSError("disk full")

    monkeypatch.setattr(OutputTopologyMutation, "save", fail_save)
    monkeypatch.setattr(
        "jasper.active_speaker.measurement.record_driver_measurement",
        lambda *_args, **_kwargs: pytest.fail(
            "driver evidence must not be recorded when identity save fails"
        ),
    )

    payload = await _ack_confirming_output_identity()
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert payload["status"] == "failed"
    assert payload["reason"] == "driver_target_identity_save_failed"
    assert "measurements" not in payload
    assert "driver_target_identity_save_failed" in {
        issue["code"] for issue in payload["issues"]
    }
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is False


def test_sound_channel_protection_route_accepts_software_guard_request(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(
        _active_speaker_mono_topology_payload(
            protection_status="required_missing", card_id=None
        )
    )

    payload = sound_setup._active_speaker_channel_protection_save_payload({
        "speaker_group_id": "mono",
        "role": "tweeter",
        "protection_status": "software_guard_requested",
    })
    saved = json.loads(path.read_text(encoding="utf-8"))
    tweeter = saved["speaker_groups"][0]["channels"][1]

    assert payload["output_topology"]["status"] == "valid"
    assert payload["topology_revision"].startswith("sha256:")
    assert payload["hardware_adoption"]["identity"].startswith("sha256:")
    assert tweeter["protection_status"] == "software_guard_requested"
    assert "tweeter_software_guard_requested" in {
        issue["code"] for issue in payload["output_topology"]["evaluation"]["warnings"]
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"speaker_group_id": "left", "role": "full_range"},
        {
            "speaker_group_id": "left",
            "role": "full_range",
            "identity_verified": "false",
        },
        [
            {
                "speaker_group_id": "left",
                "role": "full_range",
                "identity_verified": True,
            }
        ],
    ],
)
def test_sound_channel_identity_save_requires_explicit_boolean(
    monkeypatch,
    tmp_path: Path,
    raw,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(_passive_left_topology_payload())

    with pytest.raises(ValueError, match="identity|object"):
        sound_setup._active_speaker_channel_identity_save_payload(raw)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is False


def test_sound_channel_identity_http_route_rejects_non_boolean_evidence(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    sound_setup._save_output_topology_payload(_passive_left_topology_payload())

    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(
            base,
            "/active-speaker/channel-identity",
            {
                "speaker_group_id": "left",
                "role": "full_range",
                "identity_verified": "false",
            },
            expect_status=400,
        )
        payload = json.loads(resp.read().decode("utf-8"))
        saved = json.loads(path.read_text(encoding="utf-8"))

        assert "identity_verified must be a boolean" in payload["error"]
        assert saved["speaker_groups"][0]["channels"][0]["identity_verified"] is False


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
        assert get_payload["topology_revision"] == "missing"
        # Stage 2: the DAC8x declares an active outputd lane, so the route
        # resolves to that lane (not a direct-DAC route) at its full width.
        assert get_payload["active_playback_route"]["playback_device_source"] == (
            "outputd_active_lane"
        )
        assert get_payload["active_playback_route"]["transport_channel_count"] == 8

        post_resp = request_with_csrf(
            base,
            "/output-topology",
            json.dumps({
                "output_topology": get_payload["output_topology"],
                "topology_revision": get_payload["topology_revision"],
            }).encode("utf-8"),
            content_type="application/json",
        )
        post_payload = json.loads(post_resp.read().decode("utf-8"))
        assert post_payload["output_topology"]["safety"]["sound_tests_allowed"] is False
        assert post_payload["topology_revision"].startswith("sha256:")
        assert post_payload["active_playback_route"]["transport_channel_count"] == 8


def test_sound_output_topology_http_route_rejects_stale_browser_save(
    monkeypatch,
    tmp_path: Path,
):
    from jasper.output_topology import new_topology_draft, save_output_topology

    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    with sound_server(tmp_path) as base:
        old_payload = sound_setup._save_output_topology_payload(
            _active_speaker_mono_topology_payload(
                protection_status="software_guard_requested"
            )
        )
        save_output_topology(new_topology_draft(), path=path)

        resp = request_with_csrf(
            base,
            "/output-topology",
            json.dumps({
                "output_topology": old_payload["output_topology"],
                "topology_revision": old_payload["topology_revision"],
            }).encode("utf-8"),
            content_type="application/json",
            expect_status=409,
        )
        conflict = json.loads(resp.read().decode("utf-8"))
        saved = json.loads(path.read_text(encoding="utf-8"))

        assert "changed in another session" in conflict["error"]
        assert conflict["output_topology"]["speaker_groups"] == []
        assert saved["speaker_groups"] == []


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


@pytest.mark.parametrize(
    ("change", "code"),
    [
        pytest.param("topology", "topology_changed", id="another_session_saved"),
        pytest.param(
            "hardware", "detected_hardware_changed", id="the_dac_was_swapped"
        ),
    ],
)
def test_reset_refuses_a_stale_request_before_parking_or_deleting(
    monkeypatch, tmp_path: Path, change, code,
):
    """A reset carries the browser's view of both the saved layout and the
    attached hardware; either having moved refuses by code, and refusing must
    happen before anything is parked or deleted."""
    from jasper.output_topology import new_topology_draft, save_output_topology

    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH", str(tmp_path / "output_hardware.json")
    )
    save_output_topology(new_topology_draft(), path=topology_path)
    _apple_dongle_detected()
    request = sound_setup._output_topology_payload()
    before = topology_path.read_bytes()

    if change == "topology":
        save_output_topology(
            new_topology_draft(name="Newer layout"), path=topology_path
        )
        before = topology_path.read_bytes()
    else:
        _dac8x_detected()
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda *_args, **_kwargs: pytest.fail("a stale reset must not park or delete"),
    )

    with pytest.raises(sound_setup.OutputHardwareRequestConflict) as raised:
        sound_setup._reset_output_topology_payload({
            "topology_revision": request["topology_revision"],
            "detected_hardware_identity": request["hardware_adoption"]["identity"],
        })

    assert raised.value.code == code
    assert topology_path.read_bytes() == before


def test_reset_revalidates_hardware_after_parking_before_deleting(
    monkeypatch, tmp_path: Path,
):
    from jasper.output_topology import new_topology_draft, save_output_topology

    topology_path = tmp_path / "output_topology.json"
    hardware_path = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(hardware_path))
    initial = new_topology_draft(name="Initial")
    save_output_topology(initial, path=topology_path)
    _apple_dongle_detected()
    request = sound_setup._output_topology_payload()

    def park_and_commit(_topology, commit, **_kwargs):
        _dac8x_detected()
        return _RuntimeMutation(commit())

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )
    _stub_audio_stops(monkeypatch)
    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        lambda: pytest.fail("second-check conflict must not clear setup evidence"),
    )

    with pytest.raises(sound_setup.OutputHardwareRequestConflict) as raised:
        sound_setup._reset_output_topology_payload({
            "topology_revision": request["topology_revision"],
            "detected_hardware_identity": request["hardware_adoption"]["identity"],
        })

    assert raised.value.code == "detected_hardware_changed"
    assert load_output_topology() == initial


def test_reset_validation_and_empty_write_share_the_topology_transaction(
    monkeypatch, tmp_path: Path,
):
    from jasper.output_topology import (
        OutputTopologyMutation,
        new_topology_draft,
        output_topology_mutation,
        save_output_topology,
    )

    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH", str(tmp_path / "staged.yml")
    )
    save_output_topology(new_topology_draft(), path=topology_path)
    request = sound_setup._output_topology_payload()
    events: list[str] = []
    original_check = sound_setup._reset_request_hardware
    original_save = OutputTopologyMutation.save
    check_count = 0

    def assert_transaction_held():
        with pytest.raises(TimeoutError):
            with output_topology_mutation(topology_path, timeout_sec=0):
                pass

    def checked(raw, *, revision):
        nonlocal check_count
        check_count += 1
        result = original_check(raw, revision=revision)
        assert_transaction_held()
        events.append(f"check-{check_count}")
        return result

    def save_under_same_lock(self, topology):
        assert_transaction_held()
        events.append("empty-write")
        return original_save(self, topology)

    monkeypatch.setattr(sound_setup, "_reset_request_hardware", checked)
    monkeypatch.setattr(OutputTopologyMutation, "save", save_under_same_lock)
    _stub_audio_stops(monkeypatch)
    def clear_under_same_lock():
        assert_transaction_held()
        events.append("setup-cleanup")
        return {"status": "cleared", "removed": []}

    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        clear_under_same_lock,
    )

    sound_setup._reset_output_topology_payload({
        "topology_revision": request["topology_revision"],
        "detected_hardware_identity": request["hardware_adoption"]["identity"],
    })

    assert events == ["check-1", "check-2", "empty-write", "setup-cleanup"]


def test_reset_accepts_the_response_from_a_normal_save_without_reload(
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

    saved = sound_setup._save_output_topology_payload(
        _innomaker_topology_payload(active=False)
    )
    reset = sound_setup._reset_output_topology_payload({
        "topology_revision": saved["topology_revision"],
        "detected_hardware_identity": saved["hardware_adoption"]["identity"],
    })

    assert reset["saved"] is True
    assert reset["output_topology"]["speaker_groups"] == []


def test_reset_http_requires_revision_and_hardware_identity(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json")
    )
    with sound_server(tmp_path) as base:
        resp = json_post_with_csrf(
            base, "/output-topology/reset", {}, expect_status=400
        )
        payload = json.loads(resp.read().decode("utf-8"))

        assert payload["error"] == "topology_revision is required"

        current = sound_setup._output_topology_payload()
        resp = json_post_with_csrf(
            base,
            "/output-topology/reset",
            {"topology_revision": current["topology_revision"]},
            expect_status=400,
        )
        payload = json.loads(resp.read().decode("utf-8"))
        assert payload["error"] == "detected_hardware_identity is required"


def test_reset_http_reports_ambiguous_failure_with_current_topology(
    monkeypatch, tmp_path: Path,
) -> None:
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    current = sound_setup._output_topology_payload()

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
            {
                "topology_revision": current["topology_revision"],
                "detected_hardware_identity": current["hardware_adoption"]["identity"],
            },
            expect_status=502,
        )
        payload = json.loads(response.read().decode("utf-8"))

    assert "not changed" not in payload["error"]
    assert payload["reset"]["status"] == "needs_attention"
    assert payload["output_topology"]["speaker_groups"] == []
    assert payload["topology_revision"] != current["topology_revision"]


def test_reset_output_topology_payload_clears_active_setup_state(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH", str(tmp_path / "staged.yml")
    )
    initial = sound_setup._output_topology_payload()
    state_envs = {
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design.json",
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE": "preview.json",
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "staged.json",
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path-safety.json",
        "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE": "startup-load.json",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE": "commission-load.json",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE": "commission-ramp.json",
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE": "measurements.json",
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE": "baseline.json",
    }
    paths = []
    for env_name, filename in state_envs.items():
        path = tmp_path / filename
        path.write_text('{"stale": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))
        paths.append(path)
    monkeypatch.setattr(
        "jasper.output_topology_runtime.trigger_reconcile",
        lambda **_kwargs: {"ok": True},
    )
    stops = _stub_audio_stops(monkeypatch)
    monkeypatch.setattr(
        sound_setup,
        "_output_topology_payload",
        lambda: {"output_topology": {"status": "draft"}},
    )

    payload = sound_setup._reset_output_topology_payload({
        "topology_revision": initial["topology_revision"],
        "detected_hardware_identity": initial["hardware_adoption"]["identity"],
    })

    assert payload["output_topology"]["status"] == "draft"
    assert payload["reset"]["status"] == "reset"
    assert stops == [
        "summed:output_topology_reset",
        "commission:output_topology_reset",
        "safe",
    ]
    assert all(not path.exists() for path in paths)


def test_reset_cleanup_failure_keeps_new_topology_and_does_not_restore_old_graph(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jasper.output_topology import new_topology_draft, save_output_topology

    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(new_topology_draft(name="Old intent"), topology_path)
    request = sound_setup._output_topology_payload()
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

    payload = sound_setup._reset_output_topology_payload({
        "topology_revision": request["topology_revision"],
        "detected_hardware_identity": request["hardware_adoption"]["identity"],
    })

    assert events == ["park", "cleanup", "converge-new-graph"]
    assert load_output_topology(topology_path).speaker_groups == ()
    assert payload["reset"]["status"] == "needs_attention"


@pytest.mark.parametrize(
    ("reconcile", "verdict"),
    [
        (
            RECONCILE_STILL_CONVERGING,
            {
                "status": "converging",
                "message": (
                    "Speaker setup was reset and is still applying. "
                    "Check Status in a moment."
                ),
            },
        ),
        (
            RECONCILE_FAILED,
            {
                "status": "needs_attention",
                "message": (
                    "Speaker setup was reset and audio is off. JTS could not "
                    "finish setup cleanup; open Status before continuing."
                ),
            },
        ),
    ],
)
def test_reset_reports_the_reconcile_verdict(
    monkeypatch, tmp_path: Path, reconcile, verdict,
) -> None:
    from jasper.output_topology import new_topology_draft, save_output_topology

    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(new_topology_draft(name="Old intent"), topology_path)
    request = sound_setup._output_topology_payload()
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

    payload = sound_setup._reset_output_topology_payload({
        "topology_revision": request["topology_revision"],
        "detected_hardware_identity": request["hardware_adoption"]["identity"],
    })

    assert payload["reset"] == verdict


def test_active_speaker_measurement_and_baseline_http_routes_are_exposed(
    monkeypatch,
    tmp_path: Path,
):
    _set_active_speaker_state_paths(
        monkeypatch,
        tmp_path,
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
    )
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "hifiberry_dac8x")
    with sound_server(tmp_path) as base:
        measurement_resp = urllib.request.urlopen(
            f"{base}/active-speaker/measurements"
        )
        measurement_payload = json.loads(measurement_resp.read().decode("utf-8"))
        profile_resp = urllib.request.urlopen(
            f"{base}/active-speaker/baseline-profile"
        )
        profile_payload = json.loads(profile_resp.read().decode("utf-8"))

        assert measurement_payload["permissions"]["may_not_play_audio"] is True
        assert profile_payload["kind"] == "jts_active_speaker_baseline_profile_candidate"
        assert profile_payload["permissions"]["may_apply"] is False


BASELINE_CONFIG_PATH = "/var/lib/camilladsp/configs/active_speaker_baseline.yml"


def _stub_baseline_apply(monkeypatch, *, applied_profile: bool = True):
    """Stand in for the whole baseline-apply backend behind the finish handoff.

    Returns ``(apply_kwargs_seen, mux_commands)``: the kwargs of every
    ``apply_baseline_profile`` call in order, and the mux commands the restore
    issued. The fake invokes ``on_candidate_verified`` exactly as the real
    apply does, so the finish route's cleanup runs.
    """

    apply_calls: list[dict] = []
    mux_commands: list[str] = []

    async def fake_apply_baseline_profile(_topology, **kwargs):
        apply_calls.append(kwargs)
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

    def fake_mux_command(command: str) -> dict:
        mux_commands.append(command)
        return {
            "mode": "auto",
            "selected_source": None,
            "active_source": "airplay",
            "test_source": None,
        }

    monkeypatch.setattr(sound_setup, "load_output_topology", lambda: object())
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft", lambda: {}
    )
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_preview.load_crossover_preview",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.measurement.load_measurement_state",
        lambda topology: {},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.apply_baseline_profile",
        fake_apply_baseline_profile,
    )
    monkeypatch.setattr(
        sound_setup, "_commission_tone_mux_command", fake_mux_command
    )
    return apply_calls, mux_commands


def _reviewed_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_baseline_profile_payload",
        lambda **_kwargs: {"candidate_fingerprint": "reviewed-candidate"},
    )


async def test_active_speaker_baseline_apply_restores_source_auto(monkeypatch):
    apply_calls, mux_commands = _stub_baseline_apply(
        monkeypatch, applied_profile=False
    )

    payload = await sound_setup._active_speaker_baseline_profile_apply_payload(
        expected_candidate_fingerprint="reviewed-candidate",
        camilla_factory=lambda: FakeCamilla("/tmp/prior.yml"),
    )

    assert mux_commands == ["AUTO"]
    assert apply_calls[0]["expected_candidate_fingerprint"] == "reviewed-candidate"
    assert payload["source_selection_restore"]["status"] == "ok"
    assert payload["source_selection_restore"]["state"]["mode"] == "auto"


async def test_active_speaker_finish_commissioning_is_single_backend_handoff(
    monkeypatch,
):
    _reviewed_candidate(monkeypatch)
    apply_calls, mux_commands = _stub_baseline_apply(monkeypatch)

    payload = await sound_setup._active_speaker_finish_commissioning_payload(
        expected_candidate_fingerprint="reviewed-candidate",
        camilla_factory=lambda: FakeCamilla("/tmp/prior.yml"),
    )

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


async def test_active_speaker_finish_stale_candidate_skips_cleanup(monkeypatch):
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_baseline_profile_payload",
        lambda **_kwargs: {
            "candidate_fingerprint": "current-candidate",
            "permissions": {"may_apply": True},
            "issues": [],
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_summed_test_tone",
        lambda **_kwargs: pytest.fail("stale apply must not stop the summed test"),
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_baseline_profile_apply_payload",
        lambda **_kwargs: pytest.fail("stale apply must not start DSP apply"),
    )

    payload = await sound_setup._active_speaker_finish_commissioning_payload(
        expected_candidate_fingerprint="stale-candidate",
        camilla_factory=lambda: pytest.fail("stale apply must not open CamillaDSP"),
    )

    assert payload["status"] == "blocked"
    assert payload["commissioning_cleanup"] == {"status": "not_attempted"}
    assert payload["issues"][-1]["code"] == (
        "baseline_candidate_fingerprint_mismatch"
    )


async def test_active_speaker_finish_race_refusal_skips_cleanup(monkeypatch):
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_baseline_profile_payload",
        lambda **_kwargs: {"candidate_fingerprint": "reviewed-candidate"},
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_summed_test_tone",
        lambda **_kwargs: pytest.fail("refused apply must not stop the summed test"),
    )
    seen = {}

    async def refuse_after_locked_refresh(**kwargs):
        seen.update(kwargs)
        return {
            "status": "blocked",
            "profile": {"candidate_fingerprint": "newer-candidate"},
            "apply": None,
            "issues": [
                {"code": "baseline_candidate_fingerprint_mismatch"}
            ],
        }

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_baseline_profile_apply_payload",
        refuse_after_locked_refresh,
    )

    payload = await sound_setup._active_speaker_finish_commissioning_payload(
        expected_candidate_fingerprint="reviewed-candidate",
        camilla_factory=lambda: pytest.fail("refused apply must not open CamillaDSP"),
    )

    assert seen["expected_candidate_fingerprint"] == "reviewed-candidate"
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

    _reviewed_candidate(monkeypatch)
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
        expected_candidate_fingerprint="reviewed-candidate",
        camilla_factory=lambda: FakeCamilla("/tmp/prior.yml"),
    )

    assert payload["status"] == "applied"
    assert payload["commissioning_cleanup"]["ramp"]["status"] == "aborted"
    assert load_ramp_state()["pending"] is None


def test_active_speaker_crossover_preview_http_route_is_csrf_protected_no_audio(
    monkeypatch,
    tmp_path: Path,
):
    _set_active_speaker_state_paths(monkeypatch, tmp_path)
    with sound_server(tmp_path) as base:
        topology = _active_speaker_mono_topology_payload(
            protection_status="software_guard_requested",
            card_id=None,
            identity_verified=True,
        )
        research = {
            "artifact_schema_version": 1,
            "kind": "jts_active_crossover_driver_research",
            "drivers": [
                {"role": "woofer", "model": "Epique E150HE-44"},
                {
                    "role": "tweeter",
                    "model": "F110M-8",
                    "recommended_highpass_hz": 2500,
                    "do_not_test_below_hz": 1200,
                },
            ],
            "crossover_candidates": [
                {
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2500,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                }
            ],
        }
        topology_state = json.loads(
            urllib.request.urlopen(f"{base}/output-topology").read().decode("utf-8")
        )
        json_post_with_csrf(
            base,
            "/output-topology",
            {
                "output_topology": topology,
                "topology_revision": topology_state["topology_revision"],
            },
        )
        json_post_with_csrf(
            base,
            "/active-speaker/design-draft",
            {
                "expected_revision": 0,
                "operator_inputs": {},
                "driver_research": research,
            },
        )

        resp = json_post_with_csrf(base, "/active-speaker/crossover-preview", {})
        payload = json.loads(resp.read().decode("utf-8"))

        assert payload["status"] == "ready_for_protected_staging"
        assert payload["safety"]["no_audio"] is True
        assert payload["permissions"]["may_not_emit_camilla_yaml"] is True


def test_sound_module_treats_saved_tab_as_live_lane_with_flat_fallback():
    js = _SOUND_MODULE.read_text()
    set_view_start = js.index("function setView(v)")
    set_view_end = js.index("function applySavedSelection", set_view_start)
    set_view_body = js[set_view_start:set_view_end]
    reconcile_start = js.index("async function reconcileLiveSource()")
    reconcile_end = js.index("async function applyProfile", reconcile_start)
    reconcile_body = js[reconcile_start:reconcile_end]
    delete_start = js.index("async function deleteEntry(id)")
    delete_end = js.index("async function loadState()", delete_start)
    delete_body = js[delete_start:delete_end]
    load_start = js.index("async function loadState()")
    load_body = js[load_start:]

    assert "var DEFAULT_SAVED_ID = 'stock:flat';" in js
    assert "function selectedSavedEntry()" in js
    assert "function selectedSavedProfile()" in js
    assert "function requestLiveSource(options)" in js
    assert "function reconcileLiveSource()" in js
    assert "requestLiveSource({immediate: true});" in set_view_body
    assert "if (view === 'saved')" in reconcile_body
    assert "return applySavedSelection(options.okMsg, seq);" in reconcile_body
    assert "if (act === 'browse-presets') { setView('saved'); }" in js
    assert "selectedId = fallbackSavedId();" in delete_body
    assert "requestLiveSource({immediate: true});" in delete_body
    assert "selectedId = findIdFor(applied);" in load_body


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


@pytest.mark.parametrize(
    "scenario",
    [
        # The module boots in follower mode (tabs + plot absent) and renders
        # the local driver/crossover UI without fetching /state.
        "followerModeRendersLocalDriverUi",
        "confirmedOutputKeepsResetPreconditions",
        "resetPartialCleanupSurfacesWarning",
        "driverResearchImportPreservesOperatorInstalledConfiguration",
        # #2883: the handoff card renders only once a baseline plays, mints its
        # prompt server-side, and discloses a copy the declarations moved past.
        "tuningHandoffCardMintsAndGoesStale",
        "activeCrossoverFirstStepRendered",
        "componentFirstResearchFlowIsOrderedAndAdvancedIsFlat",
        "passiveMainWithSubUsesResearchableMainTargetOnly",
        "partialSavePreservesUnchosenEnclosure",
        "directCrossoverEditRefreshesProposalAndFooter",
        "tweeterTypeChangeInvalidatesCopiedResearchBinding",
        "activeSpeakerSetupTogglePersistsAcrossRender",
        # The cross-child verdict is a warning, so the notice IS the disclosure:
        # evaluate_output_topology reports speaker_group_spans_child_devices
        # without blocking the save, and nothing else tells the household. This
        # render is the whole user-facing half of #2486.
        "crossChildSpeakerGroupIsDisclosedInTheMapStep",
    ],
)
def test_the_sound_module_passes_its_harness_scenario(sound_harness_out, scenario):
    assert {scenario: True} in sound_harness_out["results"]


def test_sound_component_flow_uses_bounded_responsive_grids():
    css = _SOUND_CSS.read_text()

    # The old fixed four-column row squeezed whole details panels into a single
    # track on desktop. Components and Advanced use their own min-width-safe
    # grids, capped at two columns and explicitly stacked on phones. Advanced
    # uses the page's neutral beige instead of repeating the green input surface.
    # The outer
    # setup must also keep its step list stretched: otherwise closing every
    # disclosure makes the auto-width grid shrink-wrap its summary text.
    assert (
        "grid-template-columns: minmax(12rem, 1.4fr) "
        "repeat(3, minmax(7rem, 0.7fr))"
    ) not in css
    assert ".component-card__fields" in css
    assert ".driver-research__advanced-editor" in css
    advanced_rule = css.split(
        ".driver-research__advanced-editor {", 1
    )[1].split("}", 1)[0]
    assert "background: var(--background);" in advanced_rule
    assert ".driver-research__alignment," in css
    assert ".driver-research__evidence {" in css
    assert ".active-speaker-setup .output-setup {" in css
    output_setup_rule = css.split(
        ".active-speaker-setup .output-setup {", 1
    )[1].split("}", 1)[0]
    assert "align-items: stretch;" in output_setup_rule
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    phone_start = css.index("@media (max-width: 420px)")
    phone_css = css[phone_start:]
    assert ".component-card__fields," in phone_css
    assert ".driver-research__fields {" in phone_css
    assert "grid-template-columns: minmax(0, 1fr);" in phone_css


def test_sound_css_marks_live_sources_with_red_dots():
    js = _SOUND_MODULE.read_text()
    css = _SOUND_CSS.read_text()

    assert "btn.classList.toggle('is-live', v === view);" in js
    assert ".app-header__tabs .segmented__btn.is-live::after" in css
    assert ".profile-row__dot--on" in css
    assert "background: var(--destructive);" in css
    assert ".active-speaker-issues--warning" in css
    assert ".active-speaker-issue--blocker" in css
    assert ".active-speaker-note" in css
    assert ".output-sequence__item--needs-action .output-sequence__marker" not in css

def test_sound_module_draws_a_single_response_curve_with_no_overlays():
    js = _SOUND_MODULE.read_text()
    render_start = js.index("function renderGraph(payload, enabled)")
    render_end = js.index("  // Render the graph", render_start)
    render_body = js[render_start:render_end]

    # One line only: the summed curve + fill. No per-band component overlay is
    # drawn, even for the expanded band (its dot + width shading mark it).
    assert "drawArea(payload.preview" in render_body
    assert "drawPath(curvePts, 'curve')" in render_body
    assert "component selected" not in render_body
    assert "comp.advanced" not in render_body
    assert "bandComponent" not in render_body
    # Band dots are anchored to the summed curve and only drawn when enabled.
    assert "if (enabled) html += drawBandMarkers(curvePts);" in render_body


def test_sound_module_anchors_band_dots_to_the_summed_curve():
    js = _SOUND_MODULE.read_text()
    markers_start = js.index("function drawBandMarkers(summed)")
    markers_end = js.index("function expandedPeqBandIndex()", markers_start)
    markers_body = js[markers_start:markers_end]

    assert "var expandedBand = expandedPeqBandIndex();" in markers_body
    assert "i === expandedBand" in markers_body
    # The dot sits ON the curve (summedDbAt), not at the band's raw gain — the
    # fix for the shelf/cut "dot floats off the line" bug.
    assert "summedDbAt(summed, fx)" in markers_body
    assert "band-dot" in markers_body
    # Only the expanded band adds a guide line + width shading; no per-band
    # marker lines clutter the default view.
    assert "band-guide" in markers_body
    assert "band-marker" not in markers_body
    assert "(b.type || 'Peaking') === 'Peaking'" in markers_body

    css = _SOUND_CSS.read_text()
    assert ".band-guide" in css
    assert ".band-marker " not in css
    assert ".band-width.selected" not in css


def test_sound_module_reset_draft_and_simple_zero_detent():
    """Draft reset is the user-facing revert action, and Simple sliders get a
    tiny release-time zero detent so neutral is easy without per-band buttons."""
    js = _SOUND_MODULE.read_text()
    assert "Reset draft" in js
    assert 'data-act="reset-draft"' in js
    assert "function resetDraft()" in js
    assert "Discard" not in js
    assert "var ZERO_DETENT_DB = 0.1;" in js
    assert "Math.abs(next) <= ZERO_DETENT_DB" in js
    assert "ev.target.getAttribute('data-field')" in js


def test_sound_readouts_are_not_fake_edit_controls():
    """Readouts are display-only; exact numeric editing was intentionally not
    shipped, so they must not masquerade as text-edit buttons."""
    js = _SOUND_MODULE.read_text()
    css = _SOUND_CSS.read_text()
    assert "range__readout-value" in js
    assert "simple-col__readout-value" in js
    assert "readout-btn" not in js
    assert "readout-input" not in js
    assert "cursor: text" not in css


def test_sound_module_prefers_explicit_profile_identity_then_stock_matches():
    js = _SOUND_MODULE.read_text()
    fn_start = js.index("function findIdFor(profile)")
    fn_end = js.index("function sourceProfile()", fn_start)
    body = js[fn_start:fn_end]

    explicit_identity = body.index("profile.profile_id && entryById(profile.profile_id)")
    stock_match = body.index("e.kind === 'stock'")
    custom_match = body.index("e.kind === 'custom'")

    assert explicit_identity < stock_match < custom_match


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


def test_sound_module_hides_uncontrollable_band_controls():
    js = _SOUND_MODULE.read_text()
    band_row = js[js.index("function bandRow(band, index)"):js.index("function typeBtn(")]
    # All six band types are offered.
    for t in ("Lowshelf", "Peaking", "Highshelf", "Highpass", "Lowpass", "Notch"):
        assert "typeBtn('" + t + "'" in band_row
    # Gain is hidden for cut/notch (no gain term); Width is hidden for shelves
    # (every shelf is drawn and emitted at the fixed Butterworth SHELF_Q, so
    # the control would be inert).
    assert "gainless ? '' : rangeRow('Gain'" in band_row
    assert "shelf ? '' : rangeRow('Width'" in band_row


def test_sound_module_bounds_cut_filter_width_with_cut_max_q():
    js = _SOUND_MODULE.read_text()
    # The Width slider and its clamp use a per-type ceiling for HP/LP, sourced
    # from limits.cut_max_q (SSOT in jasper/sound/profile.py CUT_MAX_Q).
    assert "function bandQMax(type)" in js
    assert "limits.cut_max_q" in js
    assert "rangeRow('Width', band.q, limits.min_q, bandQMax(type)" in js
    assert "clamp(ev.target.value, limits.min_q, bandQMax(band.type))" in js


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

    real_save = sound_setup.save_sound_settings

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

    monkeypatch.setattr(sound_setup, "save_sound_settings", recording_save)
    monkeypatch.setattr(sound_setup, "_load_profile_config", fake_reemit)
    monkeypatch.setattr(
        sound_setup,
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
    assert emitted[-1]["trim"] == sound_setup._output_trim(profile, merged)
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
    sound_setup.save_sound_settings(initial_settings)

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
        sound_setup,
        "_sound_state_write_lock",
        observed_state_lock,
    )
    real_load_sound_settings = sound_setup.load_sound_settings

    def observed_load_sound_settings(*args, **kwargs):
        if getattr(worker_role, "value", None) in {"first", "second"}:
            with record_lock:
                settings_reads.append(observed_state_lock.owned_by_current_thread())
        return real_load_sound_settings(*args, **kwargs)

    monkeypatch.setattr(
        sound_setup,
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

    monkeypatch.setattr(sound_setup, "_load_profile_config", fake_emit)
    monkeypatch.setattr(
        sound_setup,
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
    assert live_emits[-1]["trim"] == sound_setup._output_trim(
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
        "db": -24.0,
    }
    assert len(FakeVolumeFloorToneRunner.instances) == 1
    assert FakeVolumeFloorToneRunner.instances[0].started is True
    assert fake.events[0] == (
        "volume", pytest.approx(percent_to_db(1, floor_db=-24.0)), True,
    )
    assert fake.events[1] == ("mute", False, False)
    assert fake.db == pytest.approx(-24.0)
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
    assert fake.db == pytest.approx(-36.0)
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
            "active_baseline_recompose_unavailable",
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
    monkeypatch.setattr(sound_setup, "guard_mutating_request", lambda handler: True)

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
    assert payload["reason_code"] == "active_baseline_recompose_unavailable"
    # Fail closed: active config never swapped, no prepare_failed state.
    assert fake.loaded_path is None
    assert last_dsp_apply_state() is None


def test_summed_validation_route_conflicts_while_combined_test_active(
    tmp_path,
    monkeypatch,
):
    import io
    import time

    class _LiveProc:
        def poll(self):
            return None

    monkeypatch.setattr(sound_setup, "guard_mutating_request", lambda handler: True)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_summed_validation_payload",
        lambda raw: pytest.fail("active validation conflict must not write evidence"),
    )
    now = time.monotonic()
    monkeypatch.setattr(
        sound_setup,
        "_SUMMED_TEST_TONE_SESSION",
        {
            "playback_id": "active-summed-playback",
            "process": _LiveProc(),
            "speaker_group_id": "main",
            "level_dbfs": -8.0,
            "started_monotonic": now,
            "progress_monotonic": now,
            "stop_reason": None,
        },
    )

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    Handler = sound_setup._make_handler(
        profile_path=tmp_path / "sound_profile.json",
        library_path=tmp_path / "sound_profiles.json",
        config_dir=config_dir,
        camilla_factory=lambda: None,
    )
    body = json.dumps({
        "speaker_group_id": "main",
        "outcome": "blend_ok",
        "summed_test_id": "stale-summed-playback",
    }).encode()
    raw = (
        b"POST /active-speaker/summed-validation HTTP/1.1\r\nHost: jts.local\r\n"
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
    assert b"409" in status_line, status_line
    payload = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode())
    assert payload["status"] == "active_summed_test_running"
    assert payload["reason"] == "active_summed_test_running"
    assert payload["active_summed_test"]["playback_id"] == "active-summed-playback"


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
    assert refusal.reason_code == "active_baseline_recompose_unavailable"
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


def test_rollback_teardown_converts_any_failure_into_the_household_blocker():
    """The re-mute teardown may not let ANY exception escape uncopied.

    /sound/ and /correction/ both run the combined-test re-mute from a
    ``finally`` and both import this one helper. /correction/ used to catch a
    five-entry tuple (``CamillaUnavailable``, ``OSError``, ``RuntimeError``,
    ``ValueError``, ``TypeError``), so a rollback failing with anything else —
    a ``KeyError`` out of a payload, an ``AttributeError`` off a stubbed
    controller — escaped the ``finally`` unconverted. The household then got an
    unhandled exception in place of the highest-stakes sentence in the map: the
    speaker may still be audible and nothing said so.

    Mutation: narrow the helper's `except Exception` back to
    `_COMMISSION_OPERATION_ERRORS` and the KeyError case raises here instead of
    returning the blocker.
    """
    from jasper.active_speaker.web_commissioning import (
        rollback_summed_commission_teardown,
    )

    async def _boom(exc):
        raise exc

    for exc in (KeyError("payload"), AttributeError("controller"), RuntimeError("io")):
        rollback, issue = asyncio.run(
            rollback_summed_commission_teardown(
                lambda exc=exc: _boom(exc),
                log_event_name="test.rollback",
            )
        )
        assert rollback is None, exc
        assert issue == {
            "severity": "blocker",
            "code": "summed_commission_rollback_failed",
            "message": (
                "combined test played, but JTS could not re-mute the "
                "active-speaker test path"
            ),
        }, exc

    # And the success path stays a plain pass-through with no blocker.
    async def _ok():
        return {"status": "rolled_back"}

    rollback, issue = asyncio.run(
        rollback_summed_commission_teardown(_ok, log_event_name="test.rollback")
    )
    assert rollback == {"status": "rolled_back"}
    assert issue is None


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
                    "protection_status": "present",
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

    from jasper.output_topology import save_output_topology

    topology_path = tmp_path / "output_topology.json"
    hardware_path = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(hardware_path))
    save_output_topology(
        OutputTopology.from_mapping(_ported_dual_apple_topology_raw()),
        path=topology_path,
    )
    _observe_apple_cards(tmp_path, LEFT_APPLE_SERIAL, attached_serial_b)


def _stub_repin_runtime(monkeypatch, park_kwargs: dict | None = None) -> list[str]:
    """Stand in for the audio-parking choreography a re-pin shares with save."""

    def park_and_commit(_topology, commit, **kwargs):
        if park_kwargs is not None:
            park_kwargs.update(kwargs)
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

    # One of the two was replaced, and the indexes name WHICH: lanes 2-3 are
    # the second child's, so the untouched child keeps its confirmed lanes.
    assert offered["child_count"] == 2
    assert offered["replaced_child_count"] == 1
    assert offered["reverify_output_indexes"] == [2, 3]
    assert offered["reverify_output_labels"] == [
        "Apple DAC B left",
        "Apple DAC B right",
    ]

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b=RIGHT_APPLE_SERIAL)
    assert sound_setup._output_topology_payload()["hardware_repin"] is None


def test_repin_endpoint_keeps_the_design_and_clears_what_must_be_reverified(
    monkeypatch,
    tmp_path: Path,
):
    """The saved artifact is the contract, not the response body.

    Everything a swapped unit cannot invalidate survives on disk; the swapped
    child's lanes lose identity and the pair's drift evidence is dropped.
    """

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b="NEW-DONGLE")
    park_kwargs: dict = {}
    stops = _stub_repin_runtime(monkeypatch, park_kwargs)
    request = sound_setup._output_topology_payload()

    payload = sound_setup._repin_output_topology_payload({
        "topology_revision": request["topology_revision"],
        "detected_hardware_identity": request["hardware_adoption"]["identity"],
    })

    assert payload["repin"]["status"] == "repinned"
    assert "Apple DAC B left, Apple DAC B right" in payload["repin"]["message"]
    assert stops == [
        "summed:output_topology_repin",
        "commission:output_topology_repin",
        "safe",
    ]
    # The card promises audio stays off until the outputs are re-confirmed. The
    # graph selector is identity-blind, so that promise is only true because
    # this endpoint keeps the runtime parked; see
    # test_runtime_convergence.py::test_stay_parked_skips_selection_...
    assert park_kwargs["stay_parked"] is True
    assert "confirm the re-pinned outputs" in park_kwargs["parked_reason"]

    saved = load_output_topology()
    assert [child.serial for child in saved.hardware.child_devices] == [
        LEFT_APPLE_SERIAL,
        "NEW-DONGLE",
    ]
    assert {
        (group.id, channel.role): channel.identity_verified
        for group in saved.speaker_groups
        for channel in group.channels
    } == {
        ("left", "woofer"): True,
        ("left", "tweeter"): True,
        ("right", "woofer"): False,
        ("right", "tweeter"): False,
    }
    # The design itself is untouched — this is the whole point of the flow.
    before = OutputTopology.from_mapping(_ported_dual_apple_topology_raw())
    assert saved.routing == before.routing
    assert [
        (group.id, group.kind, group.mode) for group in saved.speaker_groups
    ] == [(group.id, group.kind, group.mode) for group in before.speaker_groups]
    assert [
        (channel.role, channel.driver_style, channel.physical_output_index,
         channel.protection_status)
        for group in saved.speaker_groups
        for channel in group.channels
    ] == [
        (channel.role, channel.driver_style, channel.physical_output_index,
         channel.protection_status)
        for group in before.speaker_groups
        for channel in group.channels
    ]
    # The offer is spent: the save now matches the attached hardware.
    assert sound_setup._output_topology_payload()["hardware_repin"] is None


@pytest.mark.parametrize(
    ("reconcile", "verdict"),
    [
        (
            RECONCILE_STILL_CONVERGING,
            {
                "status": "converging",
                "message": (
                    "Pinned the new DAC and kept your speaker setup. Audio is "
                    "still applying. Check Status in a moment."
                ),
            },
        ),
        (
            RECONCILE_FAILED,
            {
                "status": "needs_attention",
                "message": (
                    "The new DAC was pinned and your speaker setup was kept, "
                    "but audio remains off. Open Status before continuing."
                ),
            },
        ),
    ],
)
def test_repin_reports_the_reconcile_verdict(
    monkeypatch, tmp_path: Path, reconcile, verdict,
):
    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b="NEW-DONGLE")
    _stub_repin_runtime(monkeypatch)
    _stub_reconcile(monkeypatch, reconcile)
    request = sound_setup._output_topology_payload()

    payload = sound_setup._repin_output_topology_payload({
        "topology_revision": request["topology_revision"],
        "detected_hardware_identity": request["hardware_adoption"]["identity"],
    })

    assert payload["repin"] == verdict


def test_repin_rejects_a_stale_request_before_parking(monkeypatch, tmp_path: Path):
    """A re-pin offered against older saved state never reaches the graph."""

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b="NEW-DONGLE")
    request = sound_setup._output_topology_payload()
    topology_path = tmp_path / "output_topology.json"
    from jasper.output_topology import save_output_topology

    renamed = _ported_dual_apple_topology_raw()
    renamed["name"] = "Living room, renamed in another session"
    save_output_topology(
        OutputTopology.from_mapping(renamed), path=topology_path
    )
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda *_args, **_kwargs: pytest.fail("stale re-pin must not park"),
    )
    before = topology_path.read_bytes()

    with pytest.raises(sound_setup.OutputHardwareRequestConflict) as raised:
        sound_setup._repin_output_topology_payload({
            "topology_revision": request["topology_revision"],
            "detected_hardware_identity": request["hardware_adoption"]["identity"],
        })

    assert raised.value.code == "topology_changed"
    assert topology_path.read_bytes() == before


def test_repin_refuses_when_the_attached_pair_is_already_pinned(
    monkeypatch,
    tmp_path: Path,
):
    """The endpoint is not a laxer door than the offer it is rendered from."""

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b=RIGHT_APPLE_SERIAL)
    request = sound_setup._output_topology_payload()
    assert request["hardware_repin"] is None
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda *_args, **_kwargs: pytest.fail("unofferable re-pin must not park"),
    )

    with pytest.raises(sound_setup.OutputHardwareRequestConflict) as raised:
        sound_setup._repin_output_topology_payload({
            "topology_revision": request["topology_revision"],
            "detected_hardware_identity": request["hardware_adoption"]["identity"],
        })

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
    from jasper.output_topology import save_output_topology

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


def test_unconfirming_a_roleful_output_parks_the_speaker_at_the_click(
    monkeypatch,
    tmp_path: Path,
):
    """#2814's A2 half: the effect lands at the click, not three deploys later.

    Marking a driver lane "not confirmed" on an armed roleful box is the
    household declaring doubt about which driver hangs where — the same hazard a
    DAC swap creates. The graph selector's invariant already refuses to re-select
    an approved graph for it, but that is the DURABLE half and would only land at
    the next restart. This is the immediate half, and it reuses the re-pin's
    exact seam rather than inventing a second mechanism.
    """

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b=RIGHT_APPLE_SERIAL)
    park_kwargs: dict = {}
    _stub_repin_runtime(monkeypatch, park_kwargs)

    sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left",
        "role": "woofer",
        "identity_verified": False,
    })

    assert park_kwargs["stay_parked"] is True
    assert "confirm it again and re-arm before audio resumes" in park_kwargs["parked_reason"]
    saved = load_output_topology()
    assert {
        (group.id, channel.role): channel.identity_verified
        for group in saved.speaker_groups
        for channel in group.channels
    }[("left", "woofer")] is False


def test_a_failed_park_still_records_the_household_s_declared_doubt(
    monkeypatch,
    tmp_path: Path,
):
    """The durable half must land even when the immediate half cannot.

    ``park_and_commit_topology`` parks BEFORE it commits, so handing this
    endpoint's save into it would mean a park failure discards the un-confirm
    entirely: the lane stays verified, ``roleful_identity_confirmed`` never
    engages, and the box keeps its approved graph across every reboot. A park
    most plausibly fails when the audio graph is already unhealthy — exactly
    when the household's doubt matters most. So the flag is saved first and the
    silence is best-effort, and the response says which half landed.
    """

    from jasper.active_speaker.runtime_contract import roleful_identity_confirmed

    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b=RIGHT_APPLE_SERIAL)
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("could not safely park audio before changing topology")
        ),
    )

    payload = sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left",
        "role": "woofer",
        "identity_verified": False,
    })

    saved = load_output_topology()
    assert {
        (group.id, channel.role): channel.identity_verified
        for group in saved.speaker_groups
        for channel in group.channels
    }[("left", "woofer")] is False
    # And therefore the durable half is armed: the selector now refuses to hand
    # this box back an approved graph on any later pass.
    assert roleful_identity_confirmed(saved) is False
    # The household is told the immediate silence did not happen.
    assert payload["identity_park"]["parked"] is False
    assert "could not silence the speaker" in payload["identity_park"]["message"]


def test_identity_writes_that_cannot_silence_a_driver_do_not_park(
    monkeypatch,
    tmp_path: Path,
):
    """The park is scoped to the confirmed -> unconfirmed edge on a roleful box.

    Confirming is the common commissioning action and must stay a plain durable
    write; a passive layout has no crossover to protect; and a box that is
    already unconfirmed is already parked, so a second un-confirm must not bounce
    outputd again.
    """

    # Confirming a lane never parks — including the second lane of a box that is
    # still unconfirmed elsewhere.
    _write_repin_fixture(monkeypatch, tmp_path, attached_serial_b=RIGHT_APPLE_SERIAL)
    park_kwargs: dict = {}
    _stub_repin_runtime(monkeypatch, park_kwargs)
    sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left", "role": "woofer", "identity_verified": True,
    })
    assert park_kwargs == {}

    # Already unconfirmed: the box is parked, so a second un-confirm is a write.
    sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left", "role": "woofer", "identity_verified": False,
    })
    assert park_kwargs["stay_parked"] is True
    park_kwargs.clear()
    sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "right", "role": "woofer", "identity_verified": False,
    })
    assert park_kwargs == {}

    # A passive layout carries no crossover: an unconfirmed lane is a channel
    # swap, not a driver hazard, and it keeps playing.
    _save_topology(monkeypatch, tmp_path, _passive_stereo_topology_raw())
    park_kwargs.clear()
    sound_setup._active_speaker_channel_identity_save_payload({
        "speaker_group_id": "left", "role": "full_range", "identity_verified": False,
    })
    assert park_kwargs == {}


@pytest.mark.parametrize(
    "baseline_profile, expected_status, expected_reason, has_prompt",
    [
        ({"applied_profile_stands": True}, "ready", None, True),
        ({"applied_profile_stands": False}, "not_ready", "no_applied_baseline", False),
        (
            {
                "applied_profile_stands": False,
                "revalidation": {"required": True, "status": "required"},
            },
            "not_ready",
            "revalidation_pending",
            False,
        ),
    ],
)
def test_tuning_handoff_follows_the_pages_applied_profile_verdict(
    monkeypatch, baseline_profile, expected_status, expected_reason, has_prompt
):
    """One verdict, read where the page reads it (ADR-0195).

    No tuning flow may be a prerequisite for USING the speaker (#2883), so a
    speaker with nothing to hand over is a named not-ready, never a failure —
    and a profile awaiting revalidation says so rather than borrowing the
    "never applied" slug.
    """
    from jasper.active_speaker import tuning_handoff

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    payload = tuning_handoff.build_tuning_handoff(
        baseline_profile=baseline_profile, design_draft={"revision": 5}
    )

    assert payload["status"] == expected_status
    assert payload["reason"] == expected_reason
    assert bool(payload["prompt"]) is has_prompt
    # The binding is minted either way: the card names the speaker while it is
    # still holding the prompt back.
    assert payload["binding"]["hostname"] == "jts7.local"
    assert payload["binding"]["design_draft_revision"] == 5
    assert payload["binding"]["declaration_url"] == "http://jts7.local/sound/setup/"


def test_tuning_handoff_prompt_binds_this_speaker_and_carries_no_credential(
    monkeypatch,
):
    """Hostname-derived, revision-stamped, and closed against credentials.

    A prompt is minted to be pasted into a third-party chat session, so every
    field in it is disclosed by construction (non-negotiable 3) — hence a
    closed key set, not a scan for words. The default ``jts.local`` must never
    leak either: it resolves to *a* box, so a printed one sends its reader to
    the wrong speaker silently.
    """
    from jasper.active_speaker import tuning_handoff
    from jasper.identity import DEFAULT_HOSTNAME

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    payload = tuning_handoff.build_tuning_handoff(
        baseline_profile={"applied_profile_stands": True},
        design_draft={"revision": 5},
    )
    prompt = payload["prompt"]

    assert set(payload["binding"]) == {
        "speaker_name",
        "hostname",
        "declaration_url",
        "crossover_url",
        "design_draft_revision",
    }
    assert "jts7.local" in prompt
    assert DEFAULT_HOSTNAME not in prompt
    assert str(payload["binding"]["design_draft_revision"]) in prompt
    # The pointer targets: the orientation verb and the program door, by their
    # installed paths. Their behaviour is theirs to own; the prompt only names
    # them, and must keep naming ones that exist.
    assert tuning_handoff.ORIENTATION_COMMAND in prompt
    assert tuning_handoff.PROGRAM_DOOR_COMMAND in prompt


def test_tuning_handoff_route_serves_the_minted_payload(tmp_path, monkeypatch):
    from jasper.active_speaker import tuning_handoff

    monkeypatch.setenv("JASPER_HOSTNAME", "jts7.local")
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_baseline_profile_payload",
        lambda *a, **k: {"applied_profile_stands": True},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft",
        lambda *a, **k: {"status": "ready_for_review", "revision": 2},
    )
    with sound_server(tmp_path) as base:
        with urllib.request.urlopen(base + "/active-speaker/tuning-handoff") as resp:
            assert resp.status == 200
            payload = json.loads(resp.read())

    assert payload["status"] == "ready"
    assert payload["binding"]["design_draft_revision"] == 2
    assert payload["prompt"] == tuning_handoff.build_tuning_handoff_prompt(
        payload["binding"]
    )
