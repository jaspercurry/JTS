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
# The phase these tests drive is the SESSION phase: it is the first argument of
# ``bind_production_play``'s seam, which branches on
# ``phase in SUMMED_SWEEP_PHASES`` — and it is also the phase that reaches
# ``provenance.stimulus.phase``, because a composed program object serves
# several phases and its own ``phase`` names only one of them. Until
# master-plan ticket 2.9 gave the stimulus family its ``PROGRAM_`` prefix, this
# file imported ``PHASE_CHECK`` from ``jasper.audio_measurement.program`` and
# was right only by accident: the two families spelled the name identically and
# valued it identically, so the wrong import produced the right string.
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_VERIFY,
)
from jasper.audio_measurement.program import (
    FrequencyBand,
    RoleBand,
    build_check_program,
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

    async def get_volume_db(self, *, best_effort: bool = False) -> float | None:
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
        self.reads.append("active_raw")
        return self.active_raw


class _FakePlan:
    def __init__(self, measurement_volume_db: float | None = -20.0) -> None:
        self.measurement_volume_db = measurement_volume_db

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
    async def __aenter__(self) -> "_FakeWindow":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeEvidenceStore:
    def __init__(self, bundle_dir: Any) -> None:
        self.bundle_dir = bundle_dir

    def identify_artifact(self, rel: str) -> Any:
        return SimpleNamespace(fingerprint="fake", sha256="b" * 64)


def _drive_one_capture(
    monkeypatch,
    tmp_path,
    *,
    phase: str,
    cam: _FakeCam,
) -> dict[str, Any] | None:
    """Play one phase then analyze one capture, returning what the carry holds.

    Real ``play_program`` for the CHECK/MEASURE branch — the graph load, the
    writer lock and the ``play_wav`` handoff run in their production order, so
    WHEN the provenance is observed is genuinely under test. Only the transport
    below ``bind_program_playback_seams`` is faked.

    **Nothing here arms observation.** There is no marker, no environment
    variable and no flag: this is a household-default session, and what it
    returns is what the banking seam would drain (``bind_position_retention``
    calls the same ``take()`` on the same recorder). A ``None`` return means
    the play seam observed nothing at all.
    """
    from jasper.active_speaker import camilla_yaml as camilla_yaml_mod
    from jasper.active_speaker.crossover_v2 import composition as composition_mod
    from jasper.active_speaker.crossover_v2 import session_graph as session_graph_mod
    from jasper.active_speaker import program_playback as playback_mod
    from jasper.audio_measurement import program as program_mod
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.correction import coordinator

    monkeypatch.setattr(coordinator, "measurement_window", lambda **kw: _FakeWindow())
    monkeypatch.setattr(program_mod, "write_program_wav", lambda path, program: None)
    monkeypatch.setattr(
        camilla_yaml_mod, "emit_active_speaker_program_config",
        lambda *a, **k: ROUTING_GRAPH_YAML,
    )

    async def _fake_aplay(bundle_dir, artifact, *, alsa_device=None, timeout_s=60.0):
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(playback_mod, "verified_program_aplay", _fake_aplay)

    # What SetConfig does on the real daemon: the running graph changes, the
    # persisted config path does NOT. Wave 6b moved that swap out of the play
    # seams and into the session graph, so the model moved with it — this pair
    # is the whole reason ``graph.kind`` exists, and stubbing it away would
    # leave the two-captures-one-path test asserting nothing.
    async def _install(self) -> str:
        cam.active_raw = ROUTING_GRAPH_YAML
        return "probe"

    async def _restore(self) -> None:
        cam.active_raw = APPLIED_GRAPH_YAML

    monkeypatch.setattr(session_graph_mod.MeasurementSessionGraph, "install", _install)
    monkeypatch.setattr(session_graph_mod.MeasurementSessionGraph, "restore", _restore)

    def _fake_seams(cam_arg, **kwargs):
        async def play_wav() -> Any:
            return SimpleNamespace(ok=True)

        async def readmit() -> Any:
            return SimpleNamespace(allowed=True, refusals=())

        return {
            "play_wav": play_wav,
            "readmit": readmit,
            "writer_lock": lambda: _FakeWindow(),
        }

    monkeypatch.setattr(composition_mod, "bind_program_playback_seams", _fake_seams)
    monkeypatch.setattr(
        pa_mod, "analyze_program_capture", lambda *a, **k: "analysis"
    )
    v2host.set_volume_plan_for_tests(_FakePlan())

    recorder = CaptureProvenanceRecorder()
    # The SECOND recorder, the one the banking seam drains. Threaded here so
    # this helper reads the carry exactly where production reads it.
    carry = CaptureProvenanceRecorder()
    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: cam,
        evidence_store=_FakeEvidenceStore(tmp_path),
        capture_session_id="cap_provenance_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
        provenance=recorder,
    )
    program = _program()
    play(phase, program)

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None,
        meta={},
        provenance=recorder,
        carry=carry,
    )
    analyze(
        program,
        SimpleNamespace(wav=_mono_wav_bytes(), setup=None, device=None),
        MeasurementPriors(crossover_fc_hz=2000.0),
        MeasurementGeometry(),
        phase=phase,
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
    assert provenance["stimulus"]["wav_sha256"] == "b" * 64
    assert provenance["graph"]["kind"] == GRAPH_KIND_PROGRAM_ROUTING


def test_two_captures_share_a_config_path_and_still_report_different_graphs(
    monkeypatch, tmp_path
):
    """THE PIN — a config label is not a graph.

    Both captures below report the SAME ``graph.config_path``, because the
    program-routing load leaves the persisted path pointing at the durable
    anchor. Anything that derived ``kind`` from that path would give both
    captures the same answer, which is precisely the reading that cost the
    2026-08-19 session its evening. ``kind`` comes from the playback branch
    that did (or did not) perform the swap, so the two differ; the running
    graph's own fingerprint differs alongside it as corroboration.
    """
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
    assert routed["graph"]["kind"] == GRAPH_KIND_PROGRAM_ROUTING
    assert summed["graph"]["kind"] == GRAPH_KIND_APPLIED
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
        monkeypatch, tmp_path, phase=PHASE_VERIFY, cam=_FakeCam(volume_db=-20.0),
    )
    assert provenance is not None
    assert _program().phase != PHASE_VERIFY
    assert provenance["stimulus"]["phase"] == PHASE_VERIFY


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
    assert provenance["graph"]["kind"] == GRAPH_KIND_PROGRAM_ROUTING
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


def test_the_engine_compose_leg_observes_and_holds_like_the_flow_leg(
    monkeypatch, tmp_path,
):
    """Wrapper parity: the engine leg's stimulus carries the same provenance
    block and the same mid-lock fader proof a flow-leg stimulus does.

    `bind_production_play`'s compose is what `TuningSession.measure` plays
    through, and `_play_body`'s two wrappers — the #2925 hold (outermost) and
    the provenance observation — must ride it identically, or a MEASURE take
    banked off the engine leg names no graph and no fader while its flow-leg
    twin names both. Asserted at the composed seams' own `play_wav`: awaiting
    it records the observation, proves (never writes) the fader, and only
    then plays.
    """
    from jasper.active_speaker import camilla_yaml as camilla_yaml_mod
    from jasper.active_speaker.crossover_v2 import composition as composition_mod
    from jasper.audio_measurement import program as program_mod
    from jasper.active_speaker.crossover_v2.contracts import (
        MEASURE_KIND_CANDIDATE,
    )
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec

    monkeypatch.setattr(program_mod, "write_program_wav", lambda path, program: None)
    monkeypatch.setattr(
        camilla_yaml_mod, "emit_active_speaker_program_config",
        lambda *a, **k: ROUTING_GRAPH_YAML,
    )
    played: list[str] = []

    def _fake_seams(cam_arg, **kwargs):
        async def play_wav() -> Any:
            played.append("played")
            return SimpleNamespace(ok=True)

        async def readmit() -> Any:
            return SimpleNamespace(allowed=True, refusals=())

        return {
            "play_wav": play_wav,
            "readmit": readmit,
            "writer_lock": lambda: _FakeWindow(),
        }

    monkeypatch.setattr(composition_mod, "bind_program_playback_seams", _fake_seams)

    class _HoldRecordingPlan(_FakePlan):
        def __init__(self) -> None:
            super().__init__()
            self.holds: list[str] = []

        async def hold_measurement_volume(
            self, get_main_volume_db: Any, *, context: str = "",
        ) -> float | None:
            self.holds.append(context)
            return await super().hold_measurement_volume(
                get_main_volume_db, context=context,
            )

    plan = _HoldRecordingPlan()
    v2host.set_volume_plan_for_tests(plan)
    cam = _FakeCam(volume_db=-20.0)
    recorder = CaptureProvenanceRecorder()
    production = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: cam,
        evidence_store=_FakeEvidenceStore(tmp_path),
        capture_session_id="engine_compose_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
        provenance=recorder,
    )

    prepared = asyncio.run(production.compose(
        spec=MeasureSpec(kind=MEASURE_KIND_CANDIDATE),
        program_for_phase=lambda phase: _program(),
    ))
    asyncio.run(prepared.seams["play_wav"]())

    carried = recorder.take()
    assert carried is not None, "the compose leg observes when a recorder is bound"
    assert carried.to_dict()["main_volume_db"] == -20.0
    assert plan.holds == ["capture:measure"], (
        "the #2925 hold runs for the engine leg's stimulus, per stimulus"
    )
    assert cam.volume_writes == [], (
        "the hold proves the declared measurement volume; it never writes it"
    )
    assert played == ["played"], "the stimulus still plays, after both wrappers"
    v2host.set_volume_plan_for_tests(None)
