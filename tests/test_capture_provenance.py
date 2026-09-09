# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A banked capture must say what it was taken THROUGH.

The 2026-08-19 jts3 forensic session is the subject: hours were spent
comparing capture levels that turned out to have been measured through two
different DSP graphs, because the capture records held neither the fader nor
any identity of the graph that was actually loaded — only a ``config_path``
label that a program-graph load deliberately never repoints.

``test_two_captures_share_a_config_path_and_still_report_different_graphs``
is this file's pin: it reproduces exactly that pair.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker.capture_provenance import (
    GRAPH_KIND_APPLIED,
    GRAPH_KIND_PROGRAM_ROUTING,
    CaptureProvenance,
    CaptureProvenanceRecorder,
    observe_capture_provenance,
    record_capture_provenance,
)
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_VERIFY,
    PHASE_VERIFY,
)
from jasper.audio_measurement.program import (
    FrequencyBand,
    RoleBand,
    build_check_program,
    build_verify_program,
)
from jasper.audio_measurement.program_analysis import (
    MeasurementGeometry,
    MeasurementPriors,
)
from jasper.web import correction_crossover_v2 as v2host

PROVENANCE_LOGGER = "jasper.active_speaker.capture_provenance"

# The durable anchor the statefile points at. It is the SAME string whichever
# graph is actually running — that is the whole defect.
ANCHOR_PATH = "/var/lib/camilladsp/configs/active_speaker_applied.yml"

APPLIED_GRAPH_YAML = (
    "devices: {samplerate: 48000}\n"
    "filters: {xover_lp: {type: BiquadCombo}}\n"
    "pipeline: [{type: Filter, channel: 0, names: [xover_lp]}]\n"
)
ROUTING_GRAPH_YAML = (
    "devices: {samplerate: 48000}\n"
    "pipeline: [{type: Mixer, name: program_routing}]\n"
)


def _mono_wav_bytes(n: int = 4800) -> bytes:
    import io

    import numpy as np
    from scipy.io import wavfile

    buf = io.BytesIO()
    wavfile.write(buf, 48000, np.zeros(n, dtype=np.int16))
    return buf.getvalue()


def _program():
    return build_check_program(
        [
            RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
            RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
        ],
        ambient_s=0.5,
        pilot_duration_s=0.3,
    )


class _FakeCam:
    """A CamillaDSP stand-in that answers the three live reads.

    ``active_raw`` is what makes this fixture worth having: a graph load
    changes it while ``config_file_path`` stays put, exactly as
    ``set_active_config_raw`` behaves on the real daemon.

    ``volume_reads`` is the second: successive fader reads return successive
    values, so a test can stage a fader that answers the play path's own hold
    (#2925) and then goes unreadable before the provenance observation a
    moment later — the one realistic way this record's ``main_volume_db``
    still lands ``null``.
    """

    def __init__(
        self,
        *,
        volume_db: float | None = -27.5,
        volume_reads: list[float | None] | None = None,
        config_path: str | None = ANCHOR_PATH,
        active_raw: str | None = APPLIED_GRAPH_YAML,
    ) -> None:
        self.volume_db = volume_db
        self._volume_reads = list(volume_reads) if volume_reads is not None else None
        self.config_path = config_path
        self.active_raw = active_raw
        self.reads: list[str] = []
        self.volume_writes: list[float] = []
        self.locked = False
        self.require_locked_reads = False

    async def get_volume_db(self, *, best_effort: bool = False) -> float | None:
        if self.require_locked_reads:
            assert self.locked
        self.reads.append("volume")
        if self._volume_reads:
            return self._volume_reads.pop(0)
        return self.volume_db

    async def set_volume_db(self, db: float, *, best_effort: bool = False) -> bool:
        self.volume_writes.append(float(db))
        self.volume_db = float(db)
        return True

    async def get_config_file_path(self, *, best_effort: bool = False) -> str | None:
        self.reads.append("config_path")
        return self.config_path

    async def get_active_config_raw(self, *, best_effort: bool = False) -> str | None:
        if self.require_locked_reads:
            assert self.locked
        self.reads.append("active_raw")
        return self.active_raw

    async def normalize_config_raw(self, text: str, *, best_effort=False) -> str:
        assert self.locked
        return text


class _FakePlan:
    def __init__(self, measurement_volume_db: float | None = -20.0) -> None:
        self.measurement_volume_db = measurement_volume_db
        self.holds: list[str] = []

    def assert_ready(self, now: Any = None) -> None:
        return None

    async def hold_measurement_volume(
        self, get_main_volume_db: Any, *, context: str = "",
    ) -> float | None:
        """The play path re-proves the declared volume per stimulus (#2925).

        Delegating to the real primitive rather than stubbing it keeps these
        provenance tests honest about what a capture's ``main_volume_db`` can
        be: a record only exists at all when the fader was proven at the
        declared volume, because the hold refuses the capture otherwise.
        """
        from jasper.active_speaker.volume_latch import hold_fader_at

        self.holds.append(context)
        if self.measurement_volume_db is None:
            return None
        return await hold_fader_at(
            self.measurement_volume_db, get_main_volume_db, context=context,
        )


# --------------------------------------------------------------------------- #
# the recorder: a one-capture handoff that never speaks for a second capture
# --------------------------------------------------------------------------- #


def test_take_consumes_so_a_second_capture_never_inherits_the_first() -> None:
    recorder = CaptureProvenanceRecorder()
    recorded = CaptureProvenance(graph_kind=GRAPH_KIND_PROGRAM_ROUTING)
    recorder.record(recorded)

    assert recorder.take() is recorded
    # No play happened in between, so there is nothing this recorder can
    # honestly say about the next capture. Absent beats stale.
    assert recorder.take() is None


# --------------------------------------------------------------------------- #
# the observation: every field from exactly one live owner
# --------------------------------------------------------------------------- #


def test_every_field_comes_from_its_live_owner() -> None:
    cam = _FakeCam(volume_db=-27.5, active_raw=ROUTING_GRAPH_YAML)
    program = _program()
    artifact = SimpleNamespace(sha256="a" * 64)

    observed = asyncio.run(
        observe_capture_provenance(
            cam=cam,
            graph_kind=GRAPH_KIND_PROGRAM_ROUTING,
            program=program,
            phase=PHASE_CHECK,
            artifact=artifact,
            volume_plan=_FakePlan(measurement_volume_db=-20.0),
        )
    )

    block = observed.to_dict()
    # The live fader, not the persisted intent: the -27.5 dB the 2026-08-19
    # session had to reconstruct from the journal.
    assert block["main_volume_db"] == -27.5
    # The plan object's held volume, not the value the play seam was bound with.
    assert block["session_volume_db"] == -20.0
    assert block["graph"]["kind"] == GRAPH_KIND_PROGRAM_ROUTING
    assert block["graph"]["config_path"] == ANCHOR_PATH
    assert block["graph"]["fingerprint"]
    assert json_fingerprint(block["graph"]["config"]) == block["graph"]["fingerprint"]
    assert block["graph"]["config"]["pipeline"] == [{"type": "Mixer", "name": "program_routing"}]
    assert cam.reads.count("active_raw") == 1
    assert block["stimulus"]["program_id"] == program.program_id
    assert block["stimulus"]["phase"] == PHASE_CHECK
    assert block["stimulus"]["wav_sha256"] == "a" * 64
    # The composer's own declared digital peak for the loudest stimulus
    # segment — reused, never recomputed off the rendered WAV.
    assert block["stimulus"]["peak_dbfs"] == max(
        segment.gain_db for segment in program.stimulus_segments()
    )


def test_the_fingerprint_is_the_running_graph_not_the_config_path() -> None:
    """Two graphs behind ONE path must fingerprint differently.

    This is the property the ``kind`` field is corroborated by: even a reader
    who distrusts the recorded kind can see the graphs differ.
    """
    program = _program()

    def fingerprint_of(active_raw: str) -> str | None:
        return asyncio.run(
            observe_capture_provenance(
                cam=_FakeCam(active_raw=active_raw),
                graph_kind=GRAPH_KIND_APPLIED,
                program=program,
                phase=PHASE_CHECK,
            )
        ).graph_fingerprint

    applied = fingerprint_of(APPLIED_GRAPH_YAML)
    routing = fingerprint_of(ROUTING_GRAPH_YAML)
    assert applied and routing
    assert applied != routing


def test_an_unreadable_surface_nulls_only_itself_and_names_itself_once(caplog) -> None:
    cam = _FakeCam(volume_db=None)  # camilla down for the fader read only

    with caplog.at_level(logging.WARNING, logger=PROVENANCE_LOGGER):
        observed = asyncio.run(
            observe_capture_provenance(
                cam=cam,
                graph_kind=GRAPH_KIND_APPLIED,
                program=_program(),
                phase=PHASE_CHECK,
                volume_plan=_FakePlan(),
            )
        )

    assert observed.main_volume_db is None
    # One dead surface does not blank the rest.
    assert observed.graph_config_path == ANCHOR_PATH
    assert observed.graph_fingerprint
    assert observed.session_volume_db == -20.0
    assert "event=active_speaker.capture_provenance" in caplog.text
    assert "unreadable=main_volume_db" in caplog.text


def test_a_closed_session_volume_is_an_answer_not_an_unreadable_field(caplog) -> None:
    """``measurement_volume_db is None`` means "no session", not "read failed"."""
    with caplog.at_level(logging.WARNING, logger=PROVENANCE_LOGGER):
        observed = asyncio.run(
            observe_capture_provenance(
                cam=_FakeCam(),
                graph_kind=GRAPH_KIND_APPLIED,
                program=_program(),
                phase=PHASE_CHECK,
                volume_plan=_FakePlan(measurement_volume_db=None),
            )
        )

    assert observed.session_volume_db is None
    assert "session_volume_db" not in caplog.text


def test_a_raising_surface_cannot_escape_into_the_capture(caplog) -> None:
    class _Exploding(_FakeCam):
        async def get_active_config_raw(self, *, best_effort: bool = False):
            raise RuntimeError("websocket died mid-read")

    with caplog.at_level(logging.WARNING, logger=PROVENANCE_LOGGER):
        observed = asyncio.run(
            observe_capture_provenance(
                cam=_Exploding(),
                graph_kind=GRAPH_KIND_PROGRAM_ROUTING,
                program=_program(),
                phase=PHASE_CHECK,
            )
        )

    assert observed.graph_fingerprint is None
    assert observed.graph_kind == GRAPH_KIND_PROGRAM_ROUTING
    assert "unreadable=graph.fingerprint" in caplog.text


def test_an_unforeseen_exception_type_still_cannot_reach_the_capture(
    monkeypatch, caplog
) -> None:
    """The blind belt is blind on purpose — pin it with a type no guard names."""
    import jasper.active_speaker.capture_provenance as provenance_mod

    class _Unforeseen(Exception):
        pass

    async def _boom(**kwargs):
        raise _Unforeseen("nothing predicted this")

    monkeypatch.setattr(provenance_mod, "observe_capture_provenance", _boom)
    recorder = CaptureProvenanceRecorder()

    with caplog.at_level(logging.WARNING, logger=PROVENANCE_LOGGER):
        asyncio.run(
            provenance_mod.record_capture_provenance(
                recorder,
                open_cam=_FakeCam,
                graph_kind=GRAPH_KIND_APPLIED,
                program=_program(),
                phase=PHASE_CHECK,
            )
        )

    assert recorder.take() is None
    assert "result=failed" in caplog.text


def test_no_recorder_is_a_silent_no_op_not_a_provenance_failure(caplog) -> None:
    """The belt owns the optionality, so a missing recorder resolves nothing.

    Without this the seams' ``| None`` would reach ``recorder.record`` as an
    ``AttributeError`` and be logged as a provenance FAILURE — a WARN about a
    defect where there is only an absent collaborator.
    """
    opened: list[str] = []

    with caplog.at_level(logging.WARNING, logger=PROVENANCE_LOGGER):
        asyncio.run(
            record_capture_provenance(
                None,
                open_cam=lambda: opened.append("cam") or _FakeCam(),
                graph_kind=GRAPH_KIND_APPLIED,
                program=_program(),
                phase=PHASE_CHECK,
            )
        )

    assert opened == []  # not even the controller was constructed
    assert caplog.text == ""


@pytest.mark.parametrize("failing", ["open_cam", "read_volume_plan"])
def test_resolving_the_cam_or_the_plan_happens_inside_the_belt(
    failing: str, caplog
) -> None:
    """The belt covers OBTAINING the owners, not just reading them.

    Passed as values these two would be evaluated in the caller's argument
    list — outside the try, on the play path — so a raise there would take the
    measurement down with it. They are resolvers for exactly that reason.
    """
    def boom():
        raise RuntimeError(f"{failing} could not be resolved")

    recorder = CaptureProvenanceRecorder()
    kwargs: dict[str, Any] = {
        "open_cam": _FakeCam, "read_volume_plan": _FakePlan,
    }
    kwargs[failing] = boom

    with caplog.at_level(logging.WARNING, logger=PROVENANCE_LOGGER):
        asyncio.run(
            record_capture_provenance(
                recorder,
                graph_kind=GRAPH_KIND_APPLIED,
                program=_program(),
                phase=PHASE_CHECK,
                **kwargs,
            )
        )

    assert recorder.take() is None
    assert "result=failed" in caplog.text


# --------------------------------------------------------------------------- #
# the host wiring: play seam observes, analyze seam carries
# --------------------------------------------------------------------------- #


class _FakeWindow:
    def __init__(self, cam):
        self.cam = cam

    async def __aenter__(self) -> "_FakeWindow":
        self.cam.locked = True
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self.cam.locked = False
        return False


class _FakeEvidenceStore:
    def __init__(self, bundle_dir: Any) -> None:
        self.bundle_dir = bundle_dir

    def identify_artifact(self, rel: str) -> Any:
        return SimpleNamespace(
            fingerprint="fake", sha256=hashlib.sha256((self.bundle_dir / rel).read_bytes()).hexdigest(),
        )


def _drive_one_capture(
    monkeypatch, tmp_path, *, phase: str, cam: _FakeCam,
    graph_scope: str | None = None, plan: _FakePlan | None = None,
) -> dict[str, Any] | None:
    """Run the shared session, composer and analyzer with hardware stand-ins."""
    from jasper import dsp_apply
    from jasper.active_speaker import program_admission, program_playback
    from jasper.active_speaker.crossover_v2 import door
    from jasper.active_speaker.crossover_v2.composition import bind_engine_seams
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.active_speaker.crossover_v2.session import TuningSession
    from jasper.audio_measurement import program_analysis as pa_mod
    from tests.engine_twin import FakeGraph, FakeRecords, FakeVolume

    scope = graph_scope or ("drivers" if phase == PHASE_CHECK else "speaker_tune")
    program = _program() if scope == "drivers" else build_verify_program(2000.0, sweep_s=0.3)
    plan = plan or _FakePlan()
    entry_graph = cam.active_raw
    cam.require_locked_reads = True
    played = []

    class Graph(FakeGraph):
        async def install(self, *args):
            fingerprint = await super().install(*args)
            selected_scope = self.scopes[-1][0] if self.scopes else "drivers"
            self.submitted = ROUTING_GRAPH_YAML if selected_scope == "drivers" else APPLIED_GRAPH_YAML
            cam.active_raw = self.submitted
            return fingerprint

        def installed_graph_yaml(self):
            return self.submitted

        async def restore(self):
            await super().restore()
            cam.active_raw = entry_graph

    async def emit(bundle_dir, artifact, *, timeout_s):
        assert cam.locked
        assert cam.reads[-1] == "active_raw"
        assert plan.holds == [f"capture:{phase}"]
        played.append(cam.active_raw)
        return SimpleNamespace(ok=True)

    class Capture:
        async def around(self, play, *, program):
            await play()
            (tmp_path / "capture.wav").write_bytes(_mono_wav_bytes())
            return "capture.wav"

    graph, records = Graph(), FakeRecords()
    monkeypatch.setattr(door, "bind_measurement_graph", lambda *a, **kw: graph)
    monkeypatch.setattr(dsp_apply, "dsp_writer_lock", lambda *a, **kw: _FakeWindow(cam))
    monkeypatch.setattr(program_playback, "verified_program_aplay", emit)
    for name in ("readmit_program_from_wav", "readmit_summed_program_from_wav"):
        monkeypatch.setattr(program_admission, name, lambda *a, **kw: SimpleNamespace(allowed=True))
    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: "analysis")
    v2host.set_volume_plan_for_tests(plan)
    recorder, carry = CaptureProvenanceRecorder(), CaptureProvenanceRecorder()
    production = v2host.bind_production_play(
        camilla_factory=lambda: cam,
        evidence_store=_FakeEvidenceStore(tmp_path),
        capture_session_id="cap_provenance_probe",
        topology=object(), preset=object(), role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test", safety_profile={}, role_targets={},
        session_volume_db=-20.0, provenance=recorder,
        program_for_phase=lambda phase: program,
    )
    spec = MeasureSpec(
        kind="verify" if phase == PHASE_VERIFY else "candidate",
        graph_scope=scope, candidate_id="candidate-fp" if scope == "candidate" else "",
        program_phase=phase,
    )
    session = TuningSession(
        session_id="cap_provenance_probe", measurement_level_db=-20.0,
        seams=bind_engine_seams(
            session_graph=production.graph, records=records,
            volume_claim=FakeVolume(proven_db=-20.0), session_volume_plan=plan,
            compose_stimulus=production.compose, capture_stimulus=Capture(),
        ),
    )

    async def run():
        async with session:
            outcome = await session.measure(spec)
            assert outcome.stimuli[0].incident == ""

    asyncio.run(run())
    assert graph.scopes == [(scope, spec.candidate_id)]
    assert played == [ROUTING_GRAPH_YAML if scope == "drivers" else APPLIED_GRAPH_YAML]
    assert cam.active_raw == entry_graph
    record, = records.banked
    assert (record["graph_scope"], record["candidate_id"], record["wav_path"]) == (
        scope, spec.candidate_id, "capture.wav",
    )
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None,
        meta={}, provenance=recorder, carry=carry,
    )
    analyze(
        program,
        SimpleNamespace(wav=(tmp_path / "capture.wav").read_bytes(), setup=None, device=None),
        MeasurementPriors(crossover_fc_hz=2000.0), MeasurementGeometry(), phase=phase,
    )
    carried = carry.take()
    return carried.to_dict() if carried is not None else None


@pytest.fixture(autouse=True)
def _reset_volume_plan():
    yield
    v2host.set_volume_plan_for_tests(None)


def test_a_household_capture_carries_provenance_with_nothing_to_arm(
    monkeypatch, tmp_path
):
    """The pin the capture-dump ring's death owes: no marker, still observed.

    While the ring existed, observation was bought only when its ENABLED
    marker was present, so an ordinary household session carried nothing and
    every banked take named no graph. This drives the real play seam with
    nothing armed — no marker, no flag, no environment — and the carry the
    banking seam drains is full.

    The fader is at the declared −20.0, so the play path's hold PROVES it and
    the capture happens. What the record carries is the level the capture was
    actually taken at, and the two fields agree because a capture that reached
    the carry is by construction one whose fader was proven — the hold refuses
    rather than writing when it is not (#2925).
    """
    cam = _FakeCam(volume_db=-20.0)
    provenance = _drive_one_capture(
        monkeypatch, tmp_path, phase=PHASE_CHECK, cam=cam,
    )
    assert provenance is not None
    assert cam.volume_writes == [], (
        "the hold proves the declared measurement volume; it never writes it"
    )
    assert provenance["main_volume_db"] == -20.0
    assert provenance["session_volume_db"] == -20.0
    program_path, = tmp_path.glob("crossover_v2/cap_provenance_probe/*_program.wav")
    assert provenance["stimulus"]["wav_sha256"] == hashlib.sha256(program_path.read_bytes()).hexdigest()
    assert provenance["graph"]["kind"] == "tuning_measurement"


def test_two_captures_share_a_config_path_and_still_report_different_graphs(
    monkeypatch, tmp_path
):
    """Graph swaps retain the config path; provenance must name the played graph."""
    # Both faders sit at the declared measurement volume, so neither capture is
    # refused by the play path's hold and both reach the carry.
    routed = _drive_one_capture(
        monkeypatch, tmp_path / "routed", phase=PHASE_CHECK,
        cam=_FakeCam(volume_db=-20.0),
    )
    summed = _drive_one_capture(
        monkeypatch, tmp_path / "summed", phase=PHASE_VERIFY,
        cam=_FakeCam(volume_db=-20.0),
    )
    assert routed is not None and summed is not None

    # The label the statefile reports is identical for both...
    assert routed["graph"]["config_path"] == ANCHOR_PATH
    assert summed["graph"]["config_path"] == ANCHOR_PATH
    # ...and yet these captures went through different transfer functions.
    assert routed["graph"]["kind"] == summed["graph"]["kind"] == "tuning_measurement"
    assert routed["graph"]["fingerprint"] != summed["graph"]["fingerprint"]


def test_the_stimulus_phase_is_the_capture_s_own_not_the_program_object_s(
    monkeypatch, tmp_path
):
    """One program object serves several phases, so it cannot name the capture.

    ``crossover_v2.programs.program_for_phase`` answers every
    ``GROUP_SUMMED_SWEEP_PHASES`` position, the compared VERIFY/entry-baseline
    pair, and MEASURE/LATERAL with ONE composed object each, by identity — so
    ``program.phase`` is the name of whichever phase composed it, and a record
    that took its label from there said "verify" for a cloud position and
    "measure" for a lateral pose. The capture's own phase is the one the play
    seam was called with, which is also the phase the banked record carries.
    """
    provenance = _drive_one_capture(
        monkeypatch, tmp_path, phase=PHASE_CLOUD_VERIFY, cam=_FakeCam(volume_db=-20.0),
    )
    assert provenance is not None
    assert build_verify_program(2000.0, sweep_s=0.3).phase != PHASE_CLOUD_VERIFY
    assert provenance["stimulus"]["phase"] == PHASE_CLOUD_VERIFY


def test_an_unreadable_fader_nulls_the_field_and_the_capture_still_lands(
    monkeypatch, tmp_path, caplog
):
    """Provenance is forensic metadata; it may never cost a household a take.

    The fader answers the play path's own hold at the declared volume and then
    goes unreadable a moment later, when this record is observed. The record
    lands with a ``null`` fader rather than the capture dying — and, because a
    record claiming a declared volume it could not read IS a contradiction, the
    #2925 tripwire says so at WARN instead of leaving the null silent.
    """
    with caplog.at_level(logging.WARNING, logger=PROVENANCE_LOGGER):
        provenance = _drive_one_capture(
            monkeypatch, tmp_path, phase=PHASE_CHECK,
            cam=_FakeCam(volume_reads=[-20.0, None]),
        )
    assert provenance is not None
    assert provenance["main_volume_db"] is None
    # The capture itself is intact — the rest of the record still landed.
    assert provenance["graph"]["kind"] == "tuning_measurement"
    assert "result=volume_disagreement" in caplog.text


def test_analyze_without_a_play_carries_no_provenance(monkeypatch):
    """A capture this recorder cannot speak for gets no block at all."""
    from jasper.audio_measurement import program_analysis as pa_mod

    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: "analysis")

    carry = CaptureProvenanceRecorder()
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None,
        meta={},
        provenance=CaptureProvenanceRecorder(),
        carry=carry,
    )
    analyze(
        _program(),
        SimpleNamespace(wav=_mono_wav_bytes(), setup=None, device=None),
        MeasurementPriors(crossover_fc_hz=2000.0),
        MeasurementGeometry(),
        phase=PHASE_CHECK,
    )
    assert carry.take() is None


@pytest.mark.parametrize("scope", ["drivers", "base", "speaker_tune", "candidate"])
def test_the_shared_engine_observes_and_holds_each_graph_scope(monkeypatch, tmp_path, scope):
    phase = PHASE_CHECK if scope == "drivers" else PHASE_CLOUD_VERIFY
    cam, plan = _FakeCam(volume_db=-20.0), _FakePlan()
    carried = _drive_one_capture(
        monkeypatch, tmp_path, phase=phase, cam=cam, graph_scope=scope, plan=plan,
    )
    assert carried is not None
    assert carried["main_volume_db"] == -20.0
    assert carried["stimulus"]["phase"] == phase
    assert plan.holds == [f"capture:{phase}"]
    assert cam.volume_writes == []
