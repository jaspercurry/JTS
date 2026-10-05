# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The executor's observable work, placement, evidence and control contract."""

from __future__ import annotations

import asyncio
import json
import math
from copy import deepcopy
from itertools import count, takewhile
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from functools import partial
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

from jasper.active_speaker import angle_capture as ac, plan_run
from jasper.active_speaker.preflight import preflight
from jasper.active_speaker.excitation_safety_plan import resolve_driver_excitation_ceilings
from jasper.active_speaker.measurement_programs import (
    Pose, Preset, preset, run_preset,
)
from jasper.active_speaker.crossover_v2 import capture_dispatch
from jasper.active_speaker.crossover_v2.programs import (
    SessionExcitation, predictive_program_for_spec, program_for_spec,
)
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.round_inputs import SetTakes, round_inputs, with_records
from jasper.active_speaker.crossover_v2.round_views.directivity import _pose as directivity_pose
from jasper.active_speaker.crossover_v2.admission import MAX_EXTRA_ATTEMPTS_PER_POSITION
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec, branch_probes
from jasper.active_speaker.crossover_v2.position_gate import POSITION_HOLD_EXPIRED_CODE, PositionGate
from jasper.active_speaker.crossover_v2.room_selection import purpose_take_records
from jasper.active_speaker.crossover_v2.session import TuningSession
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY, REASON_DRIFT_BASELINES_DISAGREE, REASON_CLIPPED, REASON_ANCHOR_AMBIGUOUS, REASON_CHANNEL_MAP_MISMATCH,
    REASON_SPL_CEILING_EXCEEDED, REASON_LEVEL_OFF_TARGET, REASON_INTERNAL_ERROR, REASON_CAPTURE_OVERRUN, REASON_LEVEL_UNSOLVED, REASON_NOT_REACHED, REASON_SNR_FLOOR,
    REASON_USER_STOPPED, REASON_LEVEL_DRIFT_AT_SESSION_GAIN, REASON_RETRIES_SPENT, TakeVerdict,
)
from jasper.active_speaker.program_admission import ProgramAdmission, ProgramAdmissionRefusal, SegmentAdmission
from jasper.active_speaker.program_playback import ProgramPlaybackRefused
from jasper.active_speaker.crossover_v2.playback_transaction import PlaybackInterrupted
from jasper.active_speaker.crossover_v2.program_transaction import ProgramForStimulus, ProgramPlaybackTransaction
from jasper.active_speaker.run_manifest import (
    RunManifest, RUN_MANIFEST_KIND, TAKE_INCOMPLETE, TAKE_MEASURED, kept_measurements,
)
from jasper.active_speaker.round_packet import write_round_packet
from jasper.active_speaker.alignment_evidence import commissioning_alignment
from jasper.active_speaker.round_copy import PLACE_MICROPHONE, coverage_lines, pose_name, round_lines
from jasper.active_speaker.round_packet_report import _pose_token
from jasper.active_speaker.capture_provenance import stimulus_peak_dbfs
from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.admission.excitation_admission import FrequencyBand
from jasper.audio_measurement.admission.playback import PlaybackObservation
from jasper.audio_measurement.level import LevelReading
from jasper.audio_measurement.program import (
    ExcitationProgram, RoleBand, build_level_probe_program, build_measure_program, build_summed_level_probe_program,
    is_level_probe,
)
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from jasper.audio_measurement.wired_capture import WIRED_POST_ROLL_S
from jasper.audio_resources.volume_owner import ClaimKind, volume_owner
from jasper.platform.json_fields import CodedFieldError
from tests.program_baseline_fixtures import banked_program_baselines  # noqa: F401
from tests.crossover_v2_fixtures import (
    FakeSeams as FlowSeams, _conductor, _loc, _measure_analysis, _phase_program, _verify_analysis, _roles,
)
from tests.crossover_v2_banked_round import bank_seat_round
from tests.engine_twin import FakeGraph, FakeSeams, FakePlay, FakeVolume, SeamFailure, open_session
from tests._log_events import event_fields
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_preflight import (
    _REAR_SUM_DB, _cardioid_trial, ready_facts,
)
from tests.test_active_speaker_measurement_door import box as box  # noqa: F401
from tests.test_crossover_v2_tuning_scope import tuning_profile as tuning_profile

_ABORTS = {SeamFailure: "seam_failed"}

def _walk(angles, candidates=("fp-a",), repeats=1):
    """Each angle's takes, each of every candidate, as a preset spreads its repeats (``request_for_preset``)."""
    return ac.AngleCaptureRequest(candidates=candidates, stops=tuple(
        ac.AngleStop(Pose(angle, 0), ac.REGIME_SUMMED, candidate_id=candidate, purpose="speaker")
        for angle in angles for _ in range(repeats) for candidate in candidates), program="tournament/express")


def _repeated(row, repeats=2):
    """A preset whose every pose takes ``repeats`` takes, as a run request's repeats ask (``resolve_plan``)."""
    return replace(row, poses=tuple(replace(pose, repeats=repeats) for pose in row.poses))


def _analysis(_record):
    return ProgramAnalysis(phase="verify", stimulus_id="test", locations=(_loc("sweep"),))


@dataclass
class _StoppingGraph(FakeGraph):
    stop_after: int = 0

    async def install(self, *args, **kwargs):
        if self.installs >= self.stop_after:
            raise SeamFailure("graph unavailable")
        return await super().install(*args, **kwargs)


class AnsweredGate(PositionGate):
    def __init__(self):
        super().__init__()
        self.grants = []
        self.pending = []
        self.progress = []

    def publish(self, progress):
        super().publish(progress)
        self.progress.append(self.published()["run"])

    def gate(self, index, attempt, entry):
        try:
            super().gate(index, attempt, entry)
        except CaptureBeginDeferred:
            pending = self.published()["pending"]
            self.pending.append(pending)
            held = (pending["index"], pending["attempt"])
            self.grants.append(held)
            self.release(*held)
            super().gate(index, attempt, entry)


class _Store:
    def __init__(self, records):
        self.records, self.snapshots = records, []

    async def bank(self, record):
        if record.get("kind") == RUN_MANIFEST_KIND:
            self.snapshots.append(deepcopy(record))
            return "crossover_v2/run/run_manifest.json"
        return await self.records.bank(record)


def _summed_captures(request):
    """A plan's summed takes, as the executor's own tests play them: its stops' specs, with no
    preparation phase and no probe."""
    specs = ac.stop_specs(request, prompts=tuple(stop.prompt for stop in ac.resolve_request(request)),
                          baseline_id=ac.BASE_CANDIDATE)
    return tuple(plan_run.PlanCapture(stop, spec) for stop, spec in zip(request.stops, specs) if spec is not None)


async def _run_gated(request, *, seams=None, gate=None, analyze=_analysis, signals=None, captures=None, **kwargs):
    fakes = seams or FakeSeams()
    manifest = RunManifest("run", _Store(fakes.records))
    async with open_session(replace(fakes, records=manifest), allocate_take_id=manifest.allocate_take_id) as (session, _):
        result = await plan_run.run_plan(request, session=session, manifest=manifest, analyze=analyze,
                                         gate=gate, aborts=_ABORTS, signals=signals, **kwargs,
                                         captures=_summed_captures(request) if captures is None else captures)
    return result, fakes


def _takes(document):
    return [take for group in document["sets"] for take in group["takes"]]


@pytest.mark.parametrize("reader", ["kept_take", "purpose_take"])
def test_a_take_that_names_no_purpose_refuses_by_its_code(tmp_path, reader):
    """No purpose is inferred from a pose kind (#2902): a banked take that
    names none refuses by that field."""
    root = bank_seat_round(tmp_path)
    session, = (root / "bundle").iterdir()
    take = next(session.rglob("positions/*.json"))
    take.write_text(json.dumps({key: value for key, value in json.loads(take.read_text()).items()
                                if key != "measurement_purpose"}))
    reads = {
        "kept_take": lambda: list(kept_measurements(session, phases=("lateral",), purposes=("room",))),
        "purpose_take": lambda: purpose_take_records(session, purpose="room"),
    }
    with pytest.raises(CodedFieldError) as refused:
        reads[reader]()
    assert refused.value.code == "field_required"


@pytest.mark.parametrize(("angles", "candidates"), [([0], ("fp-a",)), ([0, 20], ("fp-a", "fp-b")), ([0, -20, 20], ("fp-a",))])
@pytest.mark.parametrize("repeats", [1, 2, 3])
def test_a_walk_groups_configs_and_repeats_under_one_pose_grant(angles, candidates, repeats):
    request, gate = _walk(angles, candidates, repeats), AnsweredGate()
    captures = plan_run.prepare_plan_captures(request)
    result, fakes = asyncio.run(_run_gated(request, gate=gate))
    assert result.status == "complete"
    assert len(result.wall_s) == result.mic_moves == len(gate.grants) == len(angles)
    assert result.takes_measured == len(fakes.banked) == len(captures) == len(angles) * len(candidates) * repeats
    assert fakes.graph.scopes == [("candidate", c.spec.candidate_id) for c in captures]
    assert fakes.graph.restores == fakes.volume.releases == 1
    doc = json.loads(json.dumps(result.joined()))
    assert len(doc["sets"]) == len(set(candidates))
    for group in doc["sets"]:
        assert [(t["pose"]["azimuth_deg"], t["selected"]) for t in group["takes"]] == [
            (angle, True) for angle in angles for _ in range(repeats)]
    assert len({t["take_id"] for t in _takes(doc)}) == len(captures)
    assert (doc["not_measured"], doc["honoured"]["takes_refused"]) == ([], 0)
    assert all(row["budget"]["by_household"] == row["budget"]["by_speaker"] == 0 for row in gate.progress)


@pytest.mark.parametrize("read", [
    pose_name, _pose_token, lambda pose: directivity_pose({"pose": pose}),
    lambda pose: SetTakes("set", {}, ({"selected": True, "pose": pose},)).on_axis,
    lambda pose: commissioning_alignment([{"pose": pose, "base": True, "candidate_id": None}]),
], ids=["round_lines", "packet_index", "directivity", "mark_takes", "alignment"])
def test_each_reader_of_a_banked_pose_tells_the_azimuth_the_executor_wrote(read):
    banked = {degrees: plan_run._pose(SimpleNamespace(pose=Pose(degrees, 0))) for degrees in (0, 20)}
    assert banked[20]["azimuth_deg"] == 20
    assert read(banked[0]) != read(banked[20])


def test_ungated_run_needs_no_placement():
    result, _ = asyncio.run(_run_gated(_walk([0], ("fp-a", "fp-b"))))
    assert (result.status, result.mic_moves, result.takes_measured) == ("complete", 0, 2)


def test_expired_placement_banks_registry_refusal(monkeypatch):
    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    ticks = iter([0.0, 1e6])
    result, fakes = asyncio.run(_run_gated(_walk([0]), gate=PositionGate(clock=lambda: next(ticks))))
    assert (result.status, result.reason) == ("partial", POSITION_HOLD_EXPIRED_CODE)
    assert fakes.play.calls == []
    take, = result.takes
    assert take["fault"] in REASON_REGISTRY
    assert take["next"] == "stop"
    assert result.to_dict()["honoured"]["takes_refused"] == 1


@pytest.mark.parametrize("banked", [0, 1, 3])
def test_interruption_keeps_records_and_names_unmeasured_work(banked):
    result, fakes = asyncio.run(_run_gated(_walk([0, 20], ("fp-a", "fp-b")),
        seams=FakeSeams(graph=_StoppingGraph(stop_after=1 + banked)), gate=AnsweredGate()))
    assert (result.status, result.reason, result.takes_measured, result.attempts) == ("partial", "seam_failed", banked, banked + 1)
    assert len(fakes.banked) == banked
    assert result.stopped_at == {"pose_index": banked // 2, "index": banked + 1}
    assert len(result.not_measured) == 4 - banked
    assert fakes.graph.restores == fakes.volume.releases == 1


@pytest.mark.parametrize(("ok", "next_action", "gain"), [
    (True, "accept", None), (False, "retake_louder", -15),
    (False, "retake_quieter", -24), (True, "fix_and_retake", None),
])
def test_retry_recomposes_at_requested_gain_and_keeps_both_takes(monkeypatch, ok, next_action, gain):
    refusal = {"fault": REASON_CLIPPED, "next": next_action, "charge": "speaker"} if next_action != "accept" else {}
    verdicts = iter([*([TakeVerdict(ok, **refusal, next_gain_db=gain)] if refusal else []), TakeVerdict(True)])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    gate = AnsweredGate()
    result, fakes = asyncio.run(_run_gated(_walk([0]), gate=gate))
    assert fakes.play.stimulus_dbfs == ([None, gain] if refusal else [None])
    assert len(fakes.banked) == (2 if refusal else 1)
    assert gate.grants == ([(1, 1), (1, 2)] if next_action == "fix_and_retake" else [(1, 1)])
    rows = _takes(result.joined())
    assert len({(t["index"], t["stimulus_ordinal"]) for t in rows}) == 1
    assert [t["attempt"] for t in rows] == ([1, 2] if refusal else [1])
    assert [t["selected"] for t in rows] == ([False, True] if refusal else [True])
    kept = {"fault": None, "next": "accept", "charge": "none"}
    assert [{key: t["verdict"][key] for key in kept} for t in rows] == ([refusal, kept] if refusal else [kept])
    assert result.to_dict()["honoured"]["takes_refused"] == (0 if ok else 1)
    assert result.to_dict()["honoured"]["retakes"] == (1 if refusal else 0)
    assert result.status == "complete"


@pytest.mark.parametrize("charge", ["speaker", "operator"])
def test_pose_budget_counts_retries_and_bounds_automatic_work(monkeypatch, charge):
    """A placement plays at most two takes after its first, of any charge (ADR-0422)."""
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: TakeVerdict(False,
        REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge=charge))
    gate = AnsweredGate()
    result, fakes = asyncio.run(_run_gated(_walk([0]), gate=gate))
    assert result.status == "partial"
    assert len(fakes.banked) == 3
    assert gate.progress[-1]["budget"]["by_household"] == (0 if charge == "speaker" else 2)
    assert gate.progress[-1]["budget"]["left"] == 0
    assert result.reason == ""
    assert result.not_measured[0]["reason"] == REASON_DRIFT_BASELINES_DISAGREE


def _refused(fault, next_action="fix_and_retake", **evidence):
    """A take refused with ``fault``, its readings in its evidence."""
    return TakeVerdict(False, fault, evidence=evidence, next=next_action,
                       charge="operator" if next_action == "fix_and_retake" else "speaker",
                       next_gain_db=-20.0 if next_action == "retake_louder" else None)


_ALIKE, _KEPT, _DRIFT = REASON_ANCHOR_AMBIGUOUS, TakeVerdict(True), capture_dispatch.SAME_POSE_DRIFT_DB


@pytest.mark.parametrize("verdicts, unmeasured", [
    ([_refused(_ALIKE, peak_dbfs=-30.0), _refused(_ALIKE, peak_dbfs=-30.0 - _DRIFT), _KEPT], [_ALIKE]),
    ([_refused(_ALIKE, peak_dbfs=-30.0), *[_refused(_ALIKE, peak_dbfs=-30.5 - _DRIFT)] * 2, _KEPT], [_ALIKE]),
    ([_refused(_ALIKE, peak_dbfs=-30.0), *[_refused(REASON_DRIFT_BASELINES_DISAGREE, "retake_same", peak_dbfs=-30.0)] * 2,
      _KEPT], [REASON_DRIFT_BASELINES_DISAGREE]),
    ([*[_refused(REASON_LEVEL_OFF_TARGET, "retake_louder", peak_dbfs=-30.0)] * 3, _KEPT], [REASON_LEVEL_OFF_TARGET]),
    ([_refused(_ALIKE, level_db_spl=70.0, peak_dbfs=-30.0), _refused(_ALIKE, level_db_spl=71.0, peak_dbfs=-20.0), _KEPT],
     [_ALIKE]),
    ([_refused(_ALIKE, peak_dbfs=-30.0), TakeVerdict(False, next="retake_louder", charge="replay", next_gain_db=-20.0),
      _refused(_ALIKE, peak_dbfs=-30.0), _KEPT], [_ALIKE]),
    ([_refused(_ALIKE, peak_dbfs=-30.0), _KEPT, _refused(_ALIKE, peak_dbfs=-30.0), _KEPT], []),
], ids=["alike", "reading-moved", "other-fault", "level-retakes", "level-reading", "replay-between", "kept-between"])
def test_a_placement_stops_after_two_takes_refused_alike(monkeypatch, verdicts, unmeasured):
    """A take refused for a retake at its level, with the fault of the placement's last
    such refusal and a reading within ``SAME_POSE_DRIFT_DB`` of it (its level reading,
    else its peak), spends the placement as its spent extras do (ADR-0428). A moved reading, another
    fault, a level retake or a kept take resets the pair; a replay is free and does not.
    Each row is the verdict of every take its placement's two configs play."""
    answers = iter(verdicts)
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(answers))

    result, fakes = asyncio.run(_run_gated(_walk([0], ("fp-a", "fp-b")), gate=AnsweredGate()))

    assert len(fakes.play.calls) == len(verdicts)
    assert [row["reason"] for row in result.not_measured] == unmeasured


def test_fix_and_retake_needs_a_fresh_same_pose_grant(monkeypatch):
    verdicts = iter([TakeVerdict(False, REASON_ANCHOR_AMBIGUOUS, next="fix_and_retake", charge="operator"), TakeVerdict(True), TakeVerdict(True)])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    gate = AnsweredGate()
    result, _ = asyncio.run(_run_gated(_walk([0], ("fp-a", "fp-b")), gate=gate))
    assert gate.grants == [(1, 1), (1, 2)]
    assert REASON_REGISTRY[REASON_ANCHOR_AMBIGUOUS].message in gate.pending[1]["prompt"]["body"]
    assert PLACE_MICROPHONE in gate.pending[1]["prompt"]["body"]
    assert result.status == "complete"
    assert any(row["fault"] == REASON_ANCHOR_AMBIGUOUS and row["next_action"] == "fix_and_retake" for row in gate.progress)


@pytest.mark.parametrize("quality_refusal", [True, False])
def test_unresolved_stop_skips_only_capture_quality_refusals(monkeypatch, quality_refusal):
    reason = REASON_ANCHOR_AMBIGUOUS if quality_refusal else REASON_SPL_CEILING_EXCEEDED
    if quality_refusal:
        verdicts = iter([
            TakeVerdict(True),
            *(TakeVerdict(False, reason, next="fix_and_retake", charge="operator") for _ in range(3)),
            TakeVerdict(True),
        ])
        monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
        seams = FakeSeams()
    else:
        monkeypatch.setattr(plan_run, "assess", lambda *a, **k: TakeVerdict(True))
        seams = FakeSeams(play=FakePlay(script=[("restore", ""), ("ready", reason)]))

    result, fakes = asyncio.run(_run_gated(
        _walk([0, 20, 40]), seams=seams, gate=AnsweredGate() if quality_refusal else None,
    ))

    assert result.status == "partial"
    assert result.reason == ("" if quality_refusal else reason)
    assert fakes.play.bearings == ([0, 20, 20, 20, 40] if quality_refusal else [0, 20])
    assert [(stop["index"], stop["reason"]) for stop in result.not_measured] == (
        [(2, reason)] if quality_refusal else [(2, reason), (3, REASON_NOT_REACHED)])


def test_exhausted_clipped_stop_ends_the_run(monkeypatch):
    """The stop that clipped keeps the run's reason. A stop the run never began was not reached."""
    verdicts = iter([
        TakeVerdict(True),
        *(TakeVerdict(False, REASON_CLIPPED, next="retake_quieter", next_gain_db=-24,
                      charge="speaker") for _ in range(3)),
    ])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))

    result, fakes = asyncio.run(_run_gated(_walk([0, 20, 40])))

    assert result.reason == REASON_CLIPPED
    assert fakes.play.bearings == [0, *([20] * 3)]
    assert [(stop["index"], stop["reason"]) for stop in result.not_measured] == [
        (2, REASON_CLIPPED), (3, REASON_NOT_REACHED)]


def test_a_run_that_halts_after_a_kept_take_gives_the_rest_its_reason(monkeypatch):
    """The stop reason belongs to the stop the run halted at. When that take was kept, the reason is
    all the stops the run never began have."""
    monkeypatch.setattr(plan_run, "assess",
                        lambda *a, **k: TakeVerdict(True, REASON_CHANNEL_MAP_MISMATCH, next="stop"))

    result, _ = asyncio.run(_run_gated(_walk([0, 20, 40])))

    assert result.reason == REASON_CHANNEL_MAP_MISMATCH
    assert [(stop["index"], stop["reason"]) for stop in result.not_measured] == [
        (2, REASON_CHANNEL_MAP_MISMATCH), (3, REASON_CHANNEL_MAP_MISMATCH)]


@pytest.mark.parametrize("action", ["retake", "complete"])
def test_host_signals_finish_current_take_and_keep_prior_evidence(action):
    signals, gate = plan_run.RunSignals(), AnsweredGate()
    calls = 0
    def analyze(record):
        nonlocal calls
        calls += 1
        if calls == 1:
            getattr(signals, action).set()
        return _analysis(record)
    result, fakes = asyncio.run(_run_gated(_walk([0, 20]), gate=gate, analyze=analyze, signals=signals))
    if action == "retake":
        assert fakes.play.bearings == [0, 0, 20]
        assert len(fakes.banked) == 3
        assert [t["selected"] for t in _takes(result.to_dict())] == [False, True, True]
        assert result.status == "complete"
    else:
        assert fakes.play.bearings == [0]
        assert result.status == "partial"
        assert [row["reason"] for row in result.not_measured] == ["complete_requested"]


def test_progress_and_manifest_are_published_during_the_run():
    gate, seen = AnsweredGate(), []
    def analyze(record):
        seen.append(gate.published())
        return _analysis(record)
    result, _ = asyncio.run(_run_gated(_walk([0, 20], ("fp-a", "fp-b")), gate=gate, analyze=analyze))
    assert [(r["run"]["pose"], r["run"]["poses"], r["run"]["config"], r["run"]["configs"], r["run"]["attempt"])
            for r in seen] == [(1, 2, 1, 2, 1), (1, 2, 2, 2, 1), (2, 2, 1, 2, 1), (2, 2, 2, 2, 1)]
    assert [r["current"]["batch"]["ordinal"] for r in seen] == [1, 2, 1, 2]
    assert [snapshot["honoured"]["takes_measured"] for snapshot in result.records.snapshots] == [0, 1, 2, 3, 4, 4]
    assert all(s["status"] == "partial" for s in result.records.snapshots[:-1])
    assert result.records.snapshots[-1]["status"] == "complete"


@pytest.mark.parametrize(("stage", "action", "retried"), [
    ("restore", "stop", False), ("restore", "accept", False),
    ("restore", "retake_same", True), ("restore", "retake_louder", True),
    ("restore", "retake_quieter", True), ("ready", "stop", False),
])
def test_incomplete_take_obeys_verdict_and_accounts_for_remaining_stops(monkeypatch, stage, action, retried):
    verdicts = iter([TakeVerdict(False, REASON_CLIPPED, next=action, charge="operator",
                               next_gain_db=-15 if action == "retake_louder" else -24),
                     TakeVerdict(True), TakeVerdict(True)])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    seams = FakeSeams(play=FakePlay(script=[(stage, REASON_CLIPPED)]))
    result, _ = asyncio.run(_run_gated(_walk([0, 20]), seams=seams))
    assert result.status == ("complete" if retried else "partial")
    assert seams.play.bearings == ([0, 0, 20] if retried else [0])
    assert result.takes[0]["quality"]["status"] == TAKE_INCOMPLETE
    assert result.takes[0]["fault"] == REASON_CLIPPED
    assert result.takes[0]["next"] == ("stop" if action == "accept" else action)
    assert [(stop["index"], stop["reason"]) for stop in result.not_measured] == (
        [] if retried else [(1, REASON_CLIPPED), (2, REASON_NOT_REACHED)])


@pytest.mark.parametrize("failure,code,reason", [
    (None, None, ""), (SeamFailure, None, "seam_failed"), (asyncio.CancelledError, None, "cancelled"),
    *[(RuntimeError, code, "internal_error") for code in (None, "unknown_refusal", 7, [])],
    (RuntimeError, "session_level_not_ready", "session_level_not_ready"),
])
def test_timing_excludes_placement_and_all_exits_publish_terminal_state(monkeypatch, failure, code, reason):
    now = 0.0
    class MovingGate(AnsweredGate):
        def gate(self, *args):
            nonlocal now
            now += 60.0
            return super().gate(*args)
    class TimedPlay(FakePlay):
        async def run(self, **kwargs):
            nonlocal now
            now += 5.0
            if failure:
                exc = failure()
                exc.code = code
                raise exc
            return await super().run(**kwargs)
    gate, fakes = MovingGate(), FakeSeams(play=TimedPlay())
    store = _Store(fakes.records)
    manifest = RunManifest("run", store)
    event = Mock(wraps=plan_run.log_event)
    monkeypatch.setattr(plan_run, "log_event", event)
    async def run():
        async with open_session(replace(fakes, records=manifest), allocate_take_id=manifest.allocate_take_id) as (session, _):
            return await plan_run.run_plan(_walk([0]), session=session, manifest=manifest, analyze=_analysis,
                                           gate=gate, aborts=_ABORTS, clock=lambda: now,
                                           captures=_summed_captures(_walk([0])))
    if failure is RuntimeError:
        with pytest.raises(RuntimeError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    terminal = store.snapshots[-1]
    expected = "cancelled" if failure is asyncio.CancelledError else "partial" if failure else "complete"
    assert terminal["wall_s"] == [5.0]
    assert terminal["status"] == gate.published()["run"]["status"] == expected
    assert terminal["finalized"] is True
    assert terminal["reason"] == reason
    assert gate.published()["pending"] is None
    if failure:
        assert gate.published()["run"]["fault"] == terminal["reason"]
        assert gate.published()["run"]["next_action"] == "stop"
    assert event.call_args.kwargs["status"] == expected


@pytest.mark.parametrize("action", ["accept", "retake_same", "stop", "complete", "cancel"])
def test_run_never_applies_a_tune(monkeypatch, action):
    from jasper.web import correction_crossover_v2_apply
    apply = Mock(side_effect=AssertionError("apply called"))
    monkeypatch.setattr(correction_crossover_v2_apply, "apply_candidate", apply)
    monkeypatch.setattr(correction_crossover_v2_apply, "handle_v2_apply", apply)
    signals = plan_run.RunSignals()
    calls = 0
    def analyze(record):
        nonlocal calls
        calls += 1
        if calls == 1:
            if action == "cancel":
                raise asyncio.CancelledError
            if action == "complete":
                signals.complete.set()
        return _analysis(record)
    verdicts = iter([TakeVerdict(action == "accept", REASON_CLIPPED if action in {"retake_same", "stop"} else None,
                               next=action if action in {"retake_same", "stop"} else "accept", charge="speaker"), TakeVerdict(True)])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    result, _ = asyncio.run(_run_gated(_walk([0]), analyze=analyze, signals=signals))
    assert result.status in {"complete", "partial", "cancelled"}
    apply.assert_not_called()


def test_request_fingerprint_tracks_the_stated_plan():
    assert plan_run.request_fingerprint(_walk([0, 20])) == plan_run.request_fingerprint(_walk([0, 20]))
    assert plan_run.request_fingerprint(_walk([0, 20])) != plan_run.request_fingerprint(_walk([0, 21]))


def test_run_allocates_unique_take_ids_across_engine_instances(tmp_path):
    from pathlib import Path
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(mono_output_topology(), calibration_id="", sessions_dir=tmp_path)
    store = BankedRecordStore(CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]), "run")
    manifest = RunManifest("run", store)
    fakes = FakeSeams()

    class FreshEngine:
        measurement_level_db = -20
        graph_fingerprint = fakes.graph.fingerprint

        async def measure(self, spec):
            async with open_session(replace(fakes, records=manifest), allocate_take_id=manifest.allocate_take_id) as (session, _):
                return await session.measure(spec)

    result = asyncio.run(plan_run.run_plan(_walk([0, 20]), session=FreshEngine(), manifest=manifest,
                         analyze=_analysis, gate=AnsweredGate(), aborts=_ABORTS,
                         captures=_summed_captures(_walk([0, 20]))))
    root = Path(info["bundle_dir"]) / "evidence/v1/artifacts"
    document = json.loads((root / result.path).read_text())
    records = [json.loads((root / take["record_id"]).read_text()) for take in _takes(document)]
    assert len({record["take_id"] for record in records}) == 2
    assert document["status"] == "complete"


def test_retake_while_next_pose_waits_restarts_the_displayed_pose(monkeypatch):
    signals = plan_run.RunSignals()

    class RetakeGate(AnsweredGate):
        asked = False

        def gate(self, index, attempt, entry):
            if index == 2 and not self.asked:
                self.asked = True
                signals.retake.set()
                raise CaptureBeginDeferred("awaiting_position", "")
            super().gate(index, attempt, entry)

    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    result, fakes = asyncio.run(_run_gated(_walk([0, 20]), gate=RetakeGate(), signals=signals))
    assert fakes.play.bearings == [0, 20]
    assert [t["selected"] for t in _takes(result.to_dict())] == [True, True]
    assert result.status == "complete"


def test_operator_retries_are_pooled_across_configs(monkeypatch):
    clipped = TakeVerdict(False, REASON_CLIPPED, next="retake_same", charge="operator")
    verdicts = iter([clipped, TakeVerdict(True), clipped, clipped])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    result, fakes = asyncio.run(_run_gated(_walk([0], ("fp-a", "fp-b"))))
    assert len(fakes.banked) == 4
    assert result.status == "partial"


@pytest.mark.parametrize("case,status,faults", [
    ("accepted", "complete", [None]),
    ("refused", "complete", [REASON_DRIFT_BASELINES_DISAGREE, None]),
    ("analysis_error", "partial", [REASON_INTERNAL_ERROR]),
])
def test_a_take_banks_the_verdict_and_level_the_run_judged(case, status, faults):
    """The bank judges each take before it writes the record, so every banked
    take holds the verdict and level the run decided it by, a refused one and
    one whose analysis failed too (ADR-0383, ADR-0395)."""
    calls = count(1)
    def analyze(record):
        if case == "analysis_error":
            raise ValueError("bad capture")
        return replace(_analysis(record), discontinuity_samples=1024 if case == "refused" and next(calls) == 1 else 0)
    result, fakes = asyncio.run(_run_gated(_walk([0]), analyze=analyze))
    records = {record["take_id"]: record for record in fakes.banked}
    assert (result.status, [row.get("fault") for row in result.takes]) == (status, faults)
    assert set(records) == {row["take_id"] for row in result.takes}
    for row in result.takes:
        record = records[row["take_id"]]
        assert record["level"] == {**row["level"], "alignment": row["alignment"]}
        assert {key: record["verdict"][key] for key in ("ok", "fault", "next", "charge")} == {
            "ok": row["quality"]["status"] == TAKE_MEASURED, "fault": row.get("fault"), "next": row.get("next", "accept"),
            "charge": row.get("charge", "none")}


def test_an_assessor_error_ends_its_captures_assessment_then_the_run(caplog):
    """The take whose assessor raised banks a stop and is logged, and the error
    ends the run once the capture has played (ADR-0383)."""
    fakes, assessor = FakeSeams(), Mock(side_effect=RuntimeError)
    with caplog.at_level("WARNING", logger=plan_run.logger.name), pytest.raises(RuntimeError):
        asyncio.run(_run_gated(_walk([0, 20]), seams=fakes, assessor=assessor))
    assert (assessor.call_count, fakes.play.stimulus_dbfs) == (1, [None])
    assert [(record["verdict"]["fault"], record["verdict"]["evidence"]) for record in fakes.banked] == [
        (REASON_INTERNAL_ERROR, {"error_type": "RuntimeError"})]
    assert event_fields(caplog, "active_speaker.take_assessment_failed") == {
        "take_id": fakes.banked[0]["take_id"], "error_type": "RuntimeError"}


def test_a_take_banked_as_its_run_is_cancelled_is_never_assessed():
    """A cancel that lands once the stimulus played interrupts its capture: the
    take banks unassessed, nothing grades it, and the run keeps no judge (ADR-0383)."""
    class InterruptedPlay(FakePlay):
        async def run(self, **kwargs):
            await super().run(**kwargs)
            asyncio.current_task().cancel()
            raise PlaybackInterrupted(PlaybackObservation(emission="completed"), wav_path="capture.wav")

    grading = Mock()
    result, fakes = asyncio.run(_run_gated(_walk([0]), seams=FakeSeams(play=InterruptedPlay()),
                                           analyze=grading, assessor=grading))
    record, = fakes.banked
    assert (result.status, record["verdict"]["evidence"], grading.called, result.judge) == (
        "cancelled", {"assessed": False}, False, None)


@pytest.mark.parametrize("changed", [
    {}, {"pose_kind": "seat", "gating_applied": False}, {"candidate_id": "candidate"}, {"graph_fingerprint": "other"}, {"stimulus_id": "other"},
    {"level_db": -12.0}, {"stimulus_dbfs": -24.0},
    {"capture_calibration": {"applied": True, "calibration_id": "other", "curve_fingerprint": "curve"}},
    {"side": "right"}, {"role": "tweeter"},
])
def test_manifest_set_identity_tracks_capture_basis_and_spans_poses(changed):
    """Neither a pose nor the window it picks (ADR-0400) is a set boundary, nor
    the level a stimulus played at (ADR-0433)."""
    manifest = RunManifest("run", _Store(FakeSeams().records))
    record = {"candidate_id": "", "graph_fingerprint": "graph", "stimulus_id": "program",
              "level_db": -20.0, "stimulus_dbfs": -18.0, "pose_kind": "bearing", "gating_applied": True,
              "side": "left", "role": "summed"}
    async def append():
        for index, degrees in enumerate([0, 10, 20], 1):
            manifest.begin({"index": index, "pose": {"azimuth_deg": degrees}}, attempt=1, pose_index=index - 1)
            await manifest.append({**record, "take_id": manifest.allocate_take_id(), **(changed if index == 2 else {})}, f"record-{index}",
                                  TakeVerdict(True), complete=True, level_observation={})
    asyncio.run(append())
    groups = manifest.to_dict()["sets"]
    split = bool(set(changed) - {"pose_kind", "gating_applied", "stimulus_id", "stimulus_dbfs"})
    assert len(groups) == (2 if split else 1)
    poses = {take["take_id"]: take["pose"]["azimuth_deg"] for take in manifest.takes}
    assert {poses[t["take_id"]] for t in groups[0]["takes"]} == ({0, 20} if split else {0, 10, 20})


def test_a_run_given_its_level_announces_its_first_take_only():
    """A run that does not find its fader still announces itself, once (ADR-0417)."""
    result, fakes = asyncio.run(_run_gated(_walk([0, 20], repeats=2)))
    assert result.status == "complete"
    assert [call["spec"].courtesy_prelude for call in fakes.play.calls] == [True, False, False, False]


@pytest.mark.parametrize(("first", "sets", "first_gains"), [
    ({"courtesy_prelude": True}, [3, 3], (-24.0, -18.0)),
    ({"gain_plan": {"woofer": -12.0, "tweeter": -21.0}}, [3, 3], (-21.0, -12.0)),
    ({"sweep_durations": {"woofer": 0.5, "tweeter": 0.5}}, [1, 1, 2, 2], (-24.0, -18.0)),
], ids=["announced", "louder", "another-shape"])
def test_a_set_holds_one_stimulus_shape_and_each_row_its_own_level(first, sets, first_gains):
    """The take that announces a run (ADR-0417), and a take that plays louder,
    as the take a raise rides does, measure the shape the run's other takes
    measure, so they share each role's set; each row names the gain its role's
    sweeps played, never the one its record asked. A take of another shape
    starts its own sets (ADR-0433)."""
    manifest = RunManifest("run", _Store(FakeSeams().records))
    roles = [RoleBand("woofer", 0, FrequencyBand(20, 2000)), RoleBand("tweeter", 1, FrequencyBand(1500, 20000))]
    async def append():
        for index, degrees in enumerate([0, 10, 20], 1):
            manifest.begin({"index": index, "pose": {"azimuth_deg": degrees}}, attempt=1, pose_index=index - 1)
            program = build_measure_program(**{"gain_plan": {"woofer": -18.0, "tweeter": -24.0}, "roles_bands": roles,
                                               **(first if index == 1 else {})})
            await manifest.append({"take_id": manifest.allocate_take_id(), "level_db": -20.0, "stimulus_dbfs": -12.0,
                                   "program": program.to_dict()},
                                  f"record-{index}", TakeVerdict(True), complete=True, level_observation={})
    asyncio.run(append())
    groups = manifest.to_dict()["sets"]
    assert [len(group["takes"]) for group in groups] == sets
    played = {(group["capture_basis"]["role"], take["index"]): take["level"]["stimulus_dbfs"]
              for group, take in zip((group for group in groups for _ in group["takes"]), manifest.takes)}
    assert played == {("tweeter", 1): first_gains[0], ("woofer", 1): first_gains[1],
                      **{("tweeter", index): -24.0 for index in (2, 3)}, **{("woofer", index): -18.0 for index in (2, 3)}}


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("prepared", [True, False])
def test_run_resolves_one_baseline_before_any_take(monkeypatch, available, prepared):
    from jasper.active_speaker.measurement_emit import MeasurementGraphRefused
    calls = []
    def baselines():
        calls.append(True)
        if not available:
            raise MeasurementGraphRefused("measurement_baseline_unavailable", {})
        return "banked-base"
    monkeypatch.setattr("jasper.active_speaker.candidate_parts.baseline_candidate_id", baselines)
    monkeypatch.setattr(plan_run, "assess", lambda *args, **kwargs: TakeVerdict(True, next="accept"))
    request = _walk([0, 20, -20], ("base",), repeats=2)
    captures = plan_run.prepare_plan_captures(request) if prepared else None
    assert calls == []
    result, fakes = asyncio.run(_run_gated(request, captures=captures))
    assert len(calls) == 1
    if available:
        assert result.status == "complete"
        assert [spec.candidate_id for spec in result.specs.values()] == ["banked-base"] * (6 + prepared)
        assert fakes.graph.scopes == [("timing", "banked-base")] * prepared + [("candidate", "banked-base")] * 6
    else:
        assert result.reason == "measurement_baseline_unavailable"
        assert result.finalized and not result.attempts
        assert not fakes.banked


@pytest.mark.parametrize("candidate,base", [("base", True), ("fp-a", False)])
def test_manifest_stamps_base_sets_after_graph_resolution(candidate, base):
    request = _walk([0], (candidate,))
    result, _ = asyncio.run(_run_gated(request, captures=plan_run.prepare_plan_captures(request)))
    groups = result.to_dict()["sets"]
    assert groups and all(group["base"] is base for group in groups)
    assert {group["capture_basis"]["candidate_id"] for group in groups} == {
        "banked-base" if base else candidate}


@pytest.mark.parametrize("ceiling, sensitivity, reason", [
    (None, True, "walk_commissioning_stop_unset"), (80, False, "measure_spl_calibration_required"),
])
async def test_run_door_requires_a_resolved_ceiling_and_watch(tmp_path, box, ceiling, sensitivity, reason):
    from types import SimpleNamespace
    from jasper.active_speaker.crossover_v2.door import isolation_hold
    from jasper.audio_measurement.calibration import MicSensitivity

    graph = FakeGraph()
    manifest = RunManifest("run", _Store(FakeSeams().records))
    build = Mock(side_effect=AssertionError("door opened without a watch"))
    door = plan_run.RunDoor(
        isolation_hold(graph=graph, camilla_factory=lambda: box, action="test",
                       volume_state_path=tmp_path / "volume.json"), build,
        MicSensitivity(-12, 18, "1234") if sensitivity else None,
        SimpleNamespace(model_key="minidsp_umik2"), ceiling,
    )
    result = await plan_run.run_plan(replace(_walk([0]), level=ac.LevelPolicy(level_db=-14)),
                                      door=door, manifest=manifest, analyze=_analysis,
                                      aborts=_ABORTS, captures=_summed_captures(_walk([0])))
    assert (result.status, result.reason, result.takes_measured) == ("partial", reason, 0)
    build.assert_not_called()
    assert not graph.installs


@pytest.mark.parametrize("layout", ["baseline_express", "baseline_full"])
def test_a_baseline_keeps_timing_at_entry_and_reads_no_room(layout):
    """A speaker baseline takes its timing at entry, once however many takes its
    poses ask, then each driver at each pose, and no room sweep: room evidence
    comes from a room or rear seat round (ADR-0400)."""
    program = _repeated(run_preset("speaker", layout))
    captures = plan_run.prepare_plan_captures(ac.request_for_preset(program))
    assert [capture.stop.pose.azimuth_deg for capture in captures if capture.spec.graph_scope == "timing"] == [0]
    assert {capture.stop.purpose for capture in captures} == {"speaker"}
    assert [capture.stop.pose.place for capture in captures if capture.spec.program_phase == "measure"] == [
        pose.place for pose in program.poses for _ in range(2)]


def test_a_speaker_preset_walks_its_driver_stops_and_no_summed_stop_names_a_band():
    """A speaker preset plays each driver alone at each pose after the entry
    timing take, and keeps no room sweep; a room take's summed stop names no
    band of its own, so it sweeps the audio band like every summed sweep
    (ADR-0328, ADR-0400)."""
    _, safety, targets = _profile_and_targets(woofer_floor=30)
    roles = tuple(RoleBand(role, channel, resolve_driver_excitation_ceilings(
        safety, fingerprint, program_admission=True)[0])
        for channel, (role, fingerprint) in enumerate(targets.items()))
    request = ac.request_for_preset(run_preset("speaker", poses="0,-20,20"), mover=ac.MOVER_ARM)

    assert [(stop.regime, stop.purpose) for stop in request.stops] == [(ac.REGIME_PER_DRIVER, "speaker")] * 3
    captures = plan_run.prepare_plan_captures(request, roles_bands=roles)
    assert [(capture.stop.pose.azimuth_deg, capture.spec.program_phase) for capture in captures
            if capture.spec.graph_scope == "timing"] == [(0, "timing")]
    room = plan_run.prepare_plan_captures(ac.request_for_preset(run_preset("room", "room_quick"), mover=ac.MOVER_ARM),
                                          roles_bands=roles)
    assert {(capture.stop.regime, capture.spec.sweep_band_hz) for capture in room} == {(ac.REGIME_SUMMED, ())}


@pytest.mark.parametrize(("regime", "candidate", "purpose", "program", "phases", "scope"), [
    ("per_driver", "base", "speaker", "speaker/mark", ("check", "timing", "measure"), "timing"),
    ("summed", "base", "speaker", "tournament/express", ("timing", "lateral"), "timing"),
    ("summed", "candidate-a", "speaker", "speaker/mark", ("lateral",), None),
    ("per_driver", "base", "speaker", "", ("check", "measure"), None),
    ("summed", "base", "room", "room/seat", ("lateral",), None),
    ("summed", "base", "rear", "rear/express", ("lateral",), None),
    ("summed", "base", "reference", "nearfield/each", ("lateral",), None),
])
@pytest.mark.parametrize("repeats", [1, 3])
def test_inline_plan_derives_only_the_preparation_it_needs(regime, candidate, purpose, program, phases, scope, repeats):
    """The timing take is the named preset's (its ``timing_take`` flag), taken once on the base; a plan
    naming no preset takes none."""
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(Pose(20, 0), regime, candidate_id=candidate, purpose=purpose),) * repeats,
        candidates=(candidate,), program=program,
    )
    captures = plan_run.prepare_plan_captures(request)
    phase_repeats = {"check": 1, "timing": 1, "measure": repeats, "lateral": repeats}
    assert tuple(capture.spec.program_phase for capture in captures) == tuple(
        phase for phase in phases for _ in range(phase_repeats[phase]))
    assert [capture.stop.pose.azimuth_deg for capture in captures[-repeats:]] == [20] * repeats
    assert all(capture.stop.pose.azimuth_deg == 0 for capture in captures[:-repeats])
    baseline = [capture for capture in captures if capture.spec.program_phase == "timing"]
    assert all((capture.spec.graph_scope, capture.stop.regime, capture.spec.positions, capture.spec.vertical_deg)
               == (scope, "summed", (0,), 0) for capture in baseline)
    assert all(capture.spec.graph_scope == ("drivers" if regime == "per_driver" else "candidate")
               for capture in captures[-repeats:])


NEAR_FIELD = preset("nearfield/each").stimulus


def test_a_near_field_plan_asks_for_every_driver_pose_and_banks_reference_takes():
    """Each pose plays its own driver alone, the gate asks for the microphone
    at every pose (the front and rear woofer at one distance included), and
    every take banks as reference evidence at its driver (ADR-0360)."""
    layout = [(driver, mm) for driver in ("woofer", "woofer:rear") for mm in (15, 30, 15)]
    program = Preset("nearfield/each", tuple(
        Pose(0, 0, kind="close", distance_m=mm / 1000, driver=driver) for driver, mm in layout),
        purposes=("reference",), stimulus=NEAR_FIELD)
    request = ac.request_for_preset(program)
    captures = plan_run.prepare_plan_captures(request)
    gate = AnsweredGate()

    result, fakes = asyncio.run(_run_gated(request, gate=gate, captures=captures,
                                           assessor=lambda *_args, **_kwargs: TakeVerdict(True, next="accept")))

    assert [(c.spec.graph_scope, c.spec.branch_target_ids, c.spec.stimulus, c.spec.program_phase) for c in captures] == [
        ("drivers", (driver,), NEAR_FIELD, "lateral") for driver, _ in layout]
    assert result.status == "complete"
    assert len(gate.grants) == result.mic_moves == len(layout)
    assert [(take["measurement_purpose"], take["pose_driver"], take["mark_distance_m"]) for take in fakes.banked] == [
        ("reference", driver, mm / 1000) for driver, mm in layout]


_MIC = MicSensitivity(-12.0)


class _LevelRecords:
    """Hands each take to its manifest as the web host does: the program it
    played, at the peak it asked for under its ceiling, and what the microphone read."""

    def __init__(self, manifest, readings, probe_db, ceiling_db):
        self.manifest, self.readings, self.probe_db, self.ceiling_db = manifest, iter(readings), probe_db, ceiling_db

    async def bank(self, record):
        record = self.manifest.capture_record(record)
        band = RoleBand("woofer", 0, FrequencyBand(20, 2000))
        peak = min(self.probe_db if record.get("stimulus_dbfs") is None else record["stimulus_dbfs"], self.ceiling_db)
        reading = next(self.readings)
        probe = record.get("stimulus_dbfs") is None and self.manifest.specs[record["index"]].level_probe
        program = (build_level_probe_program(band, (peak,), sweep_band_hz=(20.0, 2000.0), gap_s=0.5,
                                             downstream_gain_db=0.0, channels=1)
                   if probe and record.get("pose_driver") else
                   build_summed_level_probe_program((peak,), sweep_band_hz=(20.0, 2000.0), gap_s=0.5,
                                                    downstream_gain_db=0.0) if probe else
                   build_measure_program({"woofer": peak}, (band,), repeat_count=1, sweep_durations={"woofer": 0.2}))
        record.update(
            program=program.to_dict(),
            capture_integrity={"spl": {"max_window_db_spl": reading, "loudest_half_second_db_spl": reading - 3,
                                       "ceiling_db_spl": 85.0, "sens_factor_db": _MIC.sens_factor_db}})
        return await self.manifest.bank(record)


#: The drivers the fake chain's plays compose on, each from 20 Hz.
_CHAIN_BANDS = dict.fromkeys(("woofer", "woofer:rear"), FrequencyBand(20, 4000))
_CHAIN_EXCITATION = SessionExcitation((RoleBand("woofer", 0, _CHAIN_BANDS["woofer"]),), dict.fromkeys(_CHAIN_BANDS, 0.0),
                                      0.0, None, dict.fromkeys(_CHAIN_BANDS, 8.0), target_bands=_CHAIN_BANDS)


def _chain_excitation(caps_db=None):
    """The fake chain's composer, with ``caps_db`` over its drivers' 0 dBFS caps."""
    return replace(_CHAIN_EXCITATION, caps_dbfs={**_CHAIN_EXCITATION.caps_dbfs, **(caps_db or {})})


def _heard_db(segment, sensitivity, targets, room_db=0.0):
    """What a branch program's segment reads at the fake chain: its own target alone,
    or, for the sum, every target in phase."""
    drives = (segment.role,) if segment.role else targets
    return 20 * math.log10(sum(10 ** ((segment.gain_db + sensitivity[target] + room_db) / 20) for target in drives))


class _BranchChain:
    """A fake chain at one spot, or at each bearing ``sensitivity_db`` names. One
    target alone reads its sensitivity over the peak it played, and the room adds
    ``room_gain_db`` to a play whose band, as the composer builds it, reaches under
    150 Hz. A branch take banks its real composed program, so a retake reads what
    each segment played: each branch alone reads its own drive, and their sum reads
    both in phase, as a raw rear woofer and the front woofer do in the bass."""

    def __init__(self, manifest, sensitivity_db, *, ceiling_db, play, room_gain_db, caps_db=None):
        self.manifest, self.sensitivity_db, self.ceiling_db = manifest, sensitivity_db, ceiling_db
        self.play, self.room_gain_db, self.excitation = play, room_gain_db, _chain_excitation(caps_db)

    async def bank(self, record):
        record = self.manifest.capture_record(record)
        targets, asked = record["targets"] or ["woofer"], record.get("stimulus_dbfs")
        played = self.play.calls[-1]["spec"]
        sensitivity = self.sensitivity_db.get((played.positions or (0,))[0], self.sensitivity_db)
        probe = asked is None and played.level_probe and played.graph_scope != "candidate_branches"
        floor_hz = min(segment.f1_hz for segment in program_for_spec(
            played, _CHAIN_EXCITATION, None).stimulus_segments()) if (
            played.graph_scope == "drivers") else 20.0
        room_db = self.room_gain_db if floor_hz < 150.0 else 0.0
        peak = min(-42.0 if asked is None else asked, self.ceiling_db)
        band = RoleBand(targets[0], 0, FrequencyBand(20, 2000))
        reading = 20 * math.log10(sum(10 ** ((peak + sensitivity[target] + room_db) / 20)
                                      for target in targets))
        program = (build_level_probe_program(band, (peak,), sweep_band_hz=(20.0, 2000.0), gap_s=0.5,
                                             downstream_gain_db=0.0, channels=1) if probe else
                   build_measure_program({band.role: peak}, (band,), repeat_count=1, sweep_durations={band.role: 0.2}))
        if played.graph_scope == "candidate_branches":
            program = program_for_spec(played, self.excitation, None, asked)
            reading = max(_heard_db(segment, sensitivity, targets, room_db) for segment in program.stimulus_segments())
        record.update(program=program.to_dict(), capture_integrity={"spl": {
            "max_window_db_spl": reading, "loudest_half_second_db_spl": reading, "ceiling_db_spl": 85.0,
            "sens_factor_db": _MIC.sens_factor_db}})
        return await self.manifest.bank(record)


class _RedoOnPlacementGate(AnsweredGate):
    """Presses Redo once, just after the operator confirms the first placement."""

    def __init__(self, signals):
        super().__init__()
        self.signals = signals

    def gate(self, index, attempt, entry):
        if self.signals.retake.is_set() or self.grants:
            return super().gate(index, attempt, entry)
        try:
            PositionGate.gate(self, index, attempt, entry)
        except CaptureBeginDeferred:
            held = (self.published()["pending"]["index"], self.published()["pending"]["attempt"])
            self.grants.append(held)
            self.release(*held)
            self.signals.retake.set()
            raise


def _heard_analysis(record):
    """The play's located sweeps read what the microphone heard, 30 dB over the room (ADR-0364)."""
    program = ExcitationProgram.from_dict(record["program"])
    heard = _MIC.dbfs_from_db_spl(record["capture_integrity"]["spl"]["max_window_db_spl"])
    return replace(_measure_analysis(program),
                   stimulus_levels=(LevelReading(stimulus_peak_dbfs(program), heard, heard - 30.0),))


def _run_levelled(request, readings, *, replace_at=None, ceiling_db=0.0, redo_at=(), chain=None,
                  room_gain_db=0.0, verdicts=None, redo_when_unmeasured=None, caps_db=None, stop_at=None):
    """A plan whose recordings pass, admitted by the conductor as a web run's are and
    judged on the level each take read; the microphone is re-placed at take
    ``replace_at``, and the operator presses Redo during each take in
    ``redo_at``, take 0 being just after the first placement is confirmed. A
    ``chain`` reads each take from its targets' sensitivities and the room's
    gain under 150 Hz (:class:`_BranchChain`), its drivers capped at ``caps_db``
    over 0 dBFS. ``verdicts`` answers a take by
    its number instead of its assessment, where it returns one. The operator
    presses Redo just as the planned take ``redo_when_unmeasured`` is left
    unmeasured, and Stop as take ``stop_at`` is judged."""
    fakes, takes, signals = FakeSeams(), count(1), plan_run.RunSignals()
    gate = _RedoOnPlacementGate(signals) if 0 in redo_at else AnsweredGate()
    manifest = RunManifest("run", _Store(fakes.records))
    if redo_when_unmeasured is not None:
        mark = manifest.mark_not_measured

        def mark_and_redo(index, reason):
            mark(index, reason)
            if index == redo_when_unmeasured:
                signals.retake.set()
        manifest.mark_not_measured = mark_and_redo
    captures = plan_run.prepare_plan_captures(request)
    conductor = _conductor(FlowSeams(), index_phase_map={i: c.spec.program_phase for i, c in enumerate(captures, 1)})

    def assessor(analysis, **kwargs):
        take = next(takes)
        if take in redo_at:
            signals.retake.set()
        if take == stop_at:
            signals.request_stop()
        if take == replace_at:
            return TakeVerdict(False, next="fix_and_retake", charge="operator")
        return (verdicts and verdicts(take)) or capture_dispatch.assess(analysis, **kwargs)

    async def run():
        records = (_BranchChain(manifest, chain, ceiling_db=ceiling_db, play=fakes.play, room_gain_db=room_gain_db,
                                caps_db=caps_db)
                   if chain else
                   _LevelRecords(manifest, readings, probe_db=-42.0, ceiling_db=ceiling_db))
        async with open_session(replace(fakes, records=records), allocate_take_id=manifest.allocate_take_id) as (
                session, _):
            return await plan_run.run_plan(
                request, session=session, manifest=manifest, gate=gate, aborts=_ABORTS,
                signals=signals, analyze=_heard_analysis, captures=captures, assessor=assessor,
                admit=lambda i, a, e, ledger: conductor.authorize_begin(i, a, e, executor_ledger=ledger))

    result = asyncio.run(run())
    selected = [take["selected"] for take in sorted(_takes(result.to_dict()), key=lambda take: take["take_id"])]
    return result, fakes, selected, gate


def test_a_near_field_take_levels_itself_before_it_is_kept():
    """Each placement's first attempt plays under the target and is retaken at
    the solved peak; the rest of that placement starts there, a re-placement
    starts with its probe again, and in-band re-seats are never
    sent back as drift, though each banks its reading (ADR-0361)."""
    request = ac.request_for_preset(Preset("nearfield/each", tuple(
        Pose(0, 0, repeats=repeats, kind="close", distance_m=mm / 1000, driver="woofer")
        for mm, repeats in ((15, 2), (30, 1), (15, 1))), purposes=("reference",), stimulus=NEAR_FIELD))
    readings = (66.0, 79.0, 81.0, 66.0, 80.0, 64.0, 79.0, 66.0, 82.0)

    result, fakes, selected, gate = _run_levelled(request, readings, replace_at=3)

    assert result.status == "complete"
    assert fakes.play.stimulus_dbfs == [None, -29.0, -29.0, None, -29.0, None, -27.0, None, -29.0]
    assert selected == [False, True, False, False, True, False, True, False, True]
    steps = {(p["measurement"], p["attempt"]): p["level_step"] for p in gate.progress if "level_step" in p}
    assert [step == "probe" for step in steps.values()] == [asked is None for asked in fakes.play.stimulus_dbfs]
    assert [take["level"]["loudest_half_second_db_spl"] for take in sorted(
        _takes(result.joined()), key=lambda take: take["take_id"])] == [reading - 3 for reading in readings]


@pytest.mark.parametrize("readings, asked, notices, retakes", [
    ((66.0, 80.0), [None, -29.0], [None, None], 0),
    ((66.0, 86.0, 80.0), [None, -29.0, -36.0], [None, None, REASON_LEVEL_OFF_TARGET], 1),
])
def test_a_planned_probe_is_not_a_retake_and_a_missed_level_is(readings, asked, notices, retakes):
    """A driver's probe finds its take's level, and the play after it is the take: no retake
    notice, no retake counted, none spent. A take that then misses its level is a retake, named
    by its fault (ADR-0365)."""
    request = ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, kind="close", distance_m=0.015, driver="woofer"),), purposes=("reference",), stimulus=NEAR_FIELD))

    result, fakes, _, gate = _run_levelled(request, readings)

    assert fakes.play.stimulus_dbfs == asked
    played = {(p["measurement"], p["attempt"]): p for p in gate.progress if "level_step" in p}
    assert [p["level_step"] for p in played.values()] == ["probe"] + ["levelled"] * (len(asked) - 1)
    assert [p.get("retake_reason") for p in played.values()] == notices
    assert result.to_dict()["honoured"]["retakes"] == gate.progress[-1]["retakes"] == retakes
    assert gate.progress[-1]["budget"]["by_speaker"] == retakes


@pytest.mark.parametrize("readings, stop_at, retakes", [
    ((66.0,), 1, 0), ((66.0, 86.0), 2, 0), ((66.0, 86.0, 86.0), 3, 1),
], ids=["after-the-probe", "after-a-missed-take", "after-a-retake"])
def test_a_stop_counts_no_retake_for_a_play_it_kept_from_starting(readings, stop_at, retakes):
    """A Stop pressed as a take is judged ends the run before the next play. That play is no retake: it
    never played, and a probe's next play spends none. A retake that played still counts."""
    request = ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, kind="close", distance_m=0.015, driver="woofer"),), purposes=("reference",), stimulus=NEAR_FIELD))

    result, fakes, _, gate = _run_levelled(request, readings, stop_at=stop_at)

    assert (len(fakes.play.stimulus_dbfs), result.reason) == (stop_at, REASON_USER_STOPPED)
    assert result.to_dict()["honoured"]["retakes"] == gate.progress[-1]["retakes"] == retakes


def test_a_near_field_take_its_ceiling_holds_quiet_is_kept_not_retaken():
    """A take its ceiling played under the peak it asked for is kept too quiet:
    a louder retake would replay it until the pose's retries ran out (ADR-0361)."""
    request = ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, kind="close", distance_m=0.03, driver="woofer"),), purposes=("reference",), stimulus=NEAR_FIELD))

    result, fakes, selected, _ = _run_levelled(request, (66.0, 77.0), ceiling_db=-30.0)

    assert result.status == "complete"
    assert (fakes.play.stimulus_dbfs, selected) == ([None, -29.0], [False, True])


def test_a_speaker_pose_names_its_driver_and_plays_what_the_reference_pose_plays():
    """A pose of any purpose may name its driver (ADR-0366 §1). A speaker pose
    at the mark plays exactly what the reference drivers/each pose plays: that
    driver alone on the protected drivers graph, on MEASURE's band, with no
    CHECK or timing take, at the level its probe finds (ADR-0365). Its takes
    bank under its own purpose (#5737 F1)."""
    runs = []
    for program in ("speaker/mark", "drivers/each"):
        request = ac.request_for_preset(
            run_preset(program, poses='[{"azimuth_deg": 0, "elevation_deg": 0, "driver": "woofer"}]'),
            targets=("tweeter", "woofer"))
        captures = plan_run.prepare_plan_captures(request)
        result, fakes, selected, _ = _run_levelled(request, (66.0, 80.0))
        runs.append(([capture.spec for capture in captures], fakes.play.stimulus_dbfs, selected,
                     {take["measurement_purpose"] for take in _takes(result.joined())}))
    speaker, reference = runs

    assert speaker[:3] == reference[:3]
    assert [(spec.program_phase, spec.graph_scope, spec.branch_target_ids, spec.stimulus)
            for spec in speaker[0]] == [("lateral", "drivers", ("woofer",), None)]
    assert (speaker[1][0], speaker[2]) == (None, [False, True])
    assert (speaker[3], reference[3]) == ({"speaker"}, {"reference"})


def test_a_near_field_round_shows_drivers_of_one_size_that_play_apart(caplog):
    """As on jts3, the rear woofer needs 6 dB more drive than the woofer to read
    80 dB at each distance. A placement's finding shows in the round's facts and
    lines from the next pose on; the ended round shows each in its facts, lines
    and packet lines, and logs each once (#5714)."""
    request = ac.request_for_preset(Preset("nearfield/each", tuple(
        Pose(0, 0, kind="close", distance_m=mm / 1000, driver=driver)
        for driver in ("woofer", "woofer:rear") for mm in (15, 30)), purposes=("reference",), stimulus=NEAR_FIELD))

    with caplog.at_level("WARNING", logger=plan_run.logger.name):
        result, fakes, _, gate = _run_levelled(request, (70.0, 80.0) * 2 + (60.0, 80.0) * 2)

    assert fakes.play.stimulus_dbfs == [None, -33.0] * 2 + [None, -27.0] * 2
    *live, ended = gate.progress
    assert [(finding["role"], finding["pose"]["distance_m"], finding["spread_db"])
            for finding in ended["level_mismatches"]] == [("woofer", 0.015, 6.0), ("woofer", 0.03, 6.0)]
    assert [len(facts["level_mismatches"]) for facts in live] == [int(facts["pose"] == 4) for facts in live]
    lines = set(round_lines(ended)) - set(round_lines({**ended, "level_mismatches": []}))
    assert len(lines) == 2 and lines <= set(coverage_lines({}, result.joined()))
    assert len(lines & set(round_lines(live[-1]))) == 1
    assert sum(getattr(record, "jasper_event", "") == "active_speaker.driver_level_mismatch"
               for record in caplog.records) == 2


def test_a_close_driverless_spot_turns_itself_down_once():
    """rear_behind's spot 0.1 m behind the cabinet reads louder than the mark. It
    plays one probe of its own summed sweep, and its take is turned down to 80 dB;
    the mark before it probes and levels its own set (ADR-0403, ADR-0423)."""
    result, fakes, selected, gate = _run_levelled(
        ac.request_for_preset(run_preset("rear/express", "rear_behind")), (75.0, 79.0, 92.0, 80.0))

    assert result.status == "complete"
    assert fakes.play.stimulus_dbfs == [None, -38.0, None, -55.0]
    assert selected == [False, True, False, True]
    steps = {(p["measurement"], p["attempt"]): p["level_step"] for p in gate.progress if "level_step" in p}
    assert list(steps.values()) == ["probe", "levelled", "probe", "levelled"]


def test_a_close_driverless_set_shares_one_level():
    """A close driverless set probes once, at its first take. Its repeats and its
    lateral pose play at the level that take landed and answer to their repeats,
    so a lateral that reads 4 dB under the target is not raised (ADR-0403)."""
    request = ac.request_for_preset(run_preset("rear/express", poses=json.dumps([
        {"azimuth_deg": angle, "elevation_deg": 0, "distance_m": 0.5, "repeats": 2} for angle in (0, 20)])))

    result, fakes, selected, _ = _run_levelled(request, (90.0, 80.0, 80.4, 76.0, 76.3))

    assert result.status == "complete"
    assert fakes.play.stimulus_dbfs == [None] + [-53.0] * 4
    assert selected == [False] + [True] * 4


@pytest.mark.parametrize("front_db, rear_db, room_gain_db, ask_db, alone_db", [
    (110.0, 110.0, 0.0, -37.0, (-31.0, -31.0)), (106.0, 112.0, 0.0, -39.0, (-27.0, -33.0)),
    (110.0, 110.0, 8.0, -45.0, (-39.0, -39.0))],
    ids=["equal", "rear-louder", "room-gain"])
def test_a_branch_take_at_the_mark_sums_6_db_under_its_quieter_branch(front_db, rear_db, room_gain_db, ask_db,
                                                                         alone_db):
    """At the mark, each branch plays alone the probe a driver's pose plays, on
    the drivers graph, over its take's band down to the woofer's floor, so it
    reads the room's gain under 150 Hz as the take does. The take, and the next
    take of its set, play each branch alone at its own probe's level and their
    sum 6 dB under the lower one, so a raw rear branch in phase with the front
    woofer reads at most 80 dB. The take is never levelled by its own reading
    (ADR-0403 §3, ADR-0407)."""
    request = ac.request_for_preset(run_preset("rear/pair", "speaker_mark"))

    result, fakes, selected, _ = _run_levelled(request, (), ceiling_db=-12.0, room_gain_db=room_gain_db,
                                               chain={"woofer": front_db, "woofer:rear": rear_db})

    assert result.status == "complete"
    assert [(call["spec"].graph_scope, call["spec"].branch_target_ids, call["stimulus_dbfs"],
             call["spec"].branch_levels_dbfs) for call in fakes.play.calls] == [
        ("drivers", ("woofer",), None, ()), ("drivers", ("woofer:rear",), None, ()),
        *[("candidate_branches", ("woofer", "woofer:rear"), ask_db, alone_db)] * 2]
    # The probes are never kept; each take is kept in each of its sets: both branches and their sum.
    assert selected == [False, False, *[True] * 6]
    readings = [take["level"]["loudest_half_second_db_spl"]
                for take in sorted(_takes(result.joined()), key=lambda take: take["take_id"])]
    assert max(readings[2:]) <= 80.0


def test_a_re_placed_branch_take_probes_both_branches_again():
    """A new placement of a branch set's first take starts at its first branch's
    probe again, never at the take unlevelled, and its branches play alone at
    what those probes find (ADR-0365, ADR-0403 §3, ADR-0407)."""
    request = ac.request_for_preset(run_preset("rear/pair", "speaker_mark"))

    result, fakes, _, _ = _run_levelled(request, (), ceiling_db=-12.0, replace_at=3,
                                        chain={"woofer": 110.0, "woofer:rear": 110.0})

    assert result.status == "complete"
    assert [(call["spec"].graph_scope, call["stimulus_dbfs"], call["spec"].branch_levels_dbfs)
            for call in fakes.play.calls] == [
        ("drivers", None, ()), ("drivers", None, ()), ("candidate_branches", -37.0, (-31.0, -31.0))] * 2 + [
        ("candidate_branches", -37.0, (-31.0, -31.0))]


@pytest.mark.parametrize(("poses", "chain", "takes"), [
    (None, {"woofer": 124.6, "woofer:rear": 106.0}, 2), (None, {"woofer": 110.0, "woofer:rear": 110.0}, 2),
    ("0,80", {0: {"woofer": 124.6, "woofer:rear": 106.0}, 80: {"woofer": 121.6, "woofer:rear": 118.0}}, 2)],
    ids=["the rear faces away", "in phase at one level", "a second bearing, the rear 12 dB louder there"])
def test_each_branch_alone_lands_near_80_db_and_their_sum_at_or_under_it(poses, chain, takes):
    """At the mark a cardioid's rear woofer faces away and reads 18.6 dB under the
    front for one drive. Each branch plays alone at its own probe's level, so each
    lands near 80 dB; their sum plays 6 dB under the lower level, so even in phase
    it reads at most 80 dB. Each placement probes what it plays, so at 80° the
    rear's alone segment lands there too (ADR-0407; smoke test #6113, bug 6)."""
    request = ac.request_for_preset(run_preset("rear/pair", None if poses else "speaker_mark", poses))

    result, fakes, _, _ = _run_levelled(request, (), ceiling_db=-12.0, chain=chain)

    assert result.status == "complete"
    played = [call for call in fakes.play.calls if call["spec"].graph_scope == "candidate_branches"]
    assert len(played) == takes
    for call in played:
        spec = call["spec"]
        sensitivity = chain.get(spec.positions[0], chain)
        program = program_for_spec(spec, _CHAIN_EXCITATION, None, call["stimulus_dbfs"])
        heard = {segment.segment_id: _heard_db(segment, sensitivity, spec.branch_target_ids)
                 for segment in program.stimulus_segments()}
        alone = [heard[name] for name in ("sweep_w", "sweep_t", "sweep_w_rep", "sweep_t_rep")]
        assert all(78.0 <= db <= 82.0 for db in alone) and heard["sweep_verify"] <= 80.0, heard


_REAR_UP = {"woofer": 106.0, "woofer:rear": 112.0}
_REAR_HELD = {"woofer": 124.6, "woofer:rear": 106.0}


@pytest.mark.parametrize(("chain", "caps_db", "next_", "peak_db", "first", "second"), [
    (_REAR_UP, None, "retake_quieter", -30.0, (-39.0, -27.0, -33.0), (-42.0, -30.0, -36.0)),
    (_REAR_HELD, {"woofer:rear": -30.0}, "retake_quieter", -33.01, (-51.6, -45.6, -27.0), (-54.6, -48.6, -33.01)),
    (_REAR_UP, None, "retake_louder", -23.0, (-39.0, -27.0, -33.0), (-39.0, -27.0, -33.0)),
    (_REAR_HELD, {"woofer:rear": -30.0}, "retake_louder", -26.01, (-51.6, -45.6, -27.0), (-51.6, -45.6, -30.01))],
    ids=["a clip cuts every segment 3 dB", "a cut lowers a branch its ceiling held",
         "a raise lifts nothing over its probe", "a raise of a held peak lifts nothing"])
def test_a_branch_takes_retake_moves_each_segment_from_what_it_played(chain, caps_db, next_, peak_db, first, second):
    """A branch take's assessment names its new peak, which may be a branch alone.
    The retake moves each segment from what it played: a cut lowers every segment
    by what the peak moves, even one its ceiling held; a raise lifts none over its
    own probe's level, and none when the peak played held at its ceiling (here the
    rear, capped at −30 dBFS, which then asks for what it played). Each row's next
    play, sum then each branch (ADR-0407)."""
    request = ac.request_for_preset(run_preset("rear/pair", "speaker_mark"))
    verdict = TakeVerdict(False, fault=REASON_CLIPPED if next_ == "retake_quieter" else REASON_SNR_FLOOR, next=next_,
                          charge="speaker", next_gain_db=peak_db)

    _, fakes, _, _ = _run_levelled(request, (), ceiling_db=-12.0, chain=chain, caps_db=caps_db,
                                   verdicts=lambda take: verdict if take == 3 else None)

    plays = [(call["stimulus_dbfs"], *call["spec"].branch_levels_dbfs) for call in fakes.play.calls
             if call["spec"].graph_scope == "candidate_branches"]
    assert [value for play in plays[:2] for value in play] == pytest.approx([*first, *second])


def test_a_branch_set_is_one_placement_and_the_preview_prices_its_probes():
    """Each placement probes what it plays: a branch take at a second bearing is a
    new set that probes both branches again, and the preview prices those probes;
    a repeat at one placement shares them (ADR-0407)."""
    one = ac.request_for_preset(_repeated(run_preset("rear/pair", None, "0")))
    two = ac.request_for_preset(run_preset("rear/pair", None, "0,80"))
    composer = partial(program_for_spec, excitation=_CHAIN_EXCITATION, gain_plan_db=None)
    captures = {name: plan_run.prepare_plan_captures(request) for name, request in (("one", one), ("two", two))}
    facts = {name: plan_run.schedule_facts([(plan_run._pose(capture.stop), capture.spec) for capture in rows],
                                           composer, mover="arm") for name, rows in captures.items()}
    probe_s = sum(probe.total_samples / probe.sample_rate_hz for probe in map(composer, branch_probes(
        captures["two"][-1].spec)))

    assert {name: [capture.spec.level_probe for capture in rows] for name, rows in captures.items()} == {
        "one": [True, False], "two": [True, True]}
    assert facts["two"]["estimated_seconds"] - facts["one"]["estimated_seconds"] == pytest.approx(
        probe_s + 2 * WIRED_POST_ROLL_S)


_BEHIND = [{"azimuth_deg": 0, "elevation_deg": 0},
           *({"azimuth_deg": angle, "elevation_deg": 0, "kind": "behind", "distance_m": 0.1} for angle in (0, 20))]


def test_each_graph_of_a_close_set_probes_once():
    """A close set is one candidate graph: an A/B pair at two behind spots, with
    repeats, plays one probe per graph, at that graph's first take, and the
    graph's repeats and lateral take carry its level (ADR-0406)."""
    request = ac.request_for_preset(_repeated(run_preset("rear/express", poses=_BEHIND)), candidates=("base", "trial"))

    captures = plan_run.prepare_plan_captures(request)

    behind = [(ac.candidate_identity(capture.stop.candidate_id), capture.spec.level_probe, start)
              for capture, start in zip(captures, ac.level_sets([capture.stop for capture in captures],
                                                                [capture.spec.graph_scope for capture in captures]))
              if capture.stop.pose.kind == "behind"]
    firsts = {candidate: index for index, (candidate, _, _) in reversed(list(enumerate(behind)))}
    assert [probe for _, probe, _ in behind] == [index in firsts.values() for index in range(len(behind))]
    assert sorted(firsts) == ["base", "trial"]
    assert len({start for _, _, start in behind}) == 2


@pytest.mark.parametrize(("purposes", "probes"), [(("room", "speaker"), [True, True]),
                                                  (("rear", "room"), [True, False])])
def test_a_close_set_is_the_graph_its_takes_play(purposes, probes):
    """Two purposes at one close spot share a probe only when their takes play
    one graph: an in-room take on the base and every rear take play the room and
    bass layers cleared, a speaker take plays none cleared (ADR-0406, ADR-0429,
    ADR-0436)."""
    pose = Pose(0, 0, kind="behind", distance_m=0.1)
    request = ac.AngleCaptureRequest(stops=tuple(ac.AngleStop(pose, ac.REGIME_SUMMED, purpose=purpose)
                                                 for purpose in purposes))

    assert [capture.spec.level_probe for capture in plan_run.prepare_plan_captures(request)] == probes


def test_a_branch_run_of_two_candidates_shares_its_drivers_probes():
    """A branch set's probes play each branch alone on the drivers graph, so a
    second candidate there would find the same levels: the run stays one set,
    probed once (ADR-0406)."""
    pose = Pose(0, 0, kind="behind", distance_m=0.1)
    request = ac.AngleCaptureRequest(stops=tuple(
        ac.AngleStop(pose, ac.REGIME_BRANCHES, candidate_id=name, purpose="rear", branch_pair="front_rear")
        for name in ("a" * 64, "b" * 64)), candidates=("a" * 64, "b" * 64))
    roles = tuple(RoleBand(role, channel, band) for channel, (role, band) in enumerate(_RUN_BANDS.items()))

    captures = plan_run.prepare_plan_captures(request, roles_bands=roles)

    assert [capture.spec.level_probe for capture in captures] == [True, False]
    assert ac.level_sets([capture.stop for capture in captures],
                         [capture.spec.graph_scope for capture in captures]) == (0, 0)


def test_a_cardioid_on_off_trial_behind_the_cabinet_lands_each_graph_at_80_db(monkeypatch, tuning_profile):
    """Behind the cabinet the trial that plays the rear woofer reads 15 dB over
    the base that mutes it. Each graph's first take there probes and levels
    itself, so both land at 80 ± 2 dB, under the 85 dB stop, at both behind spots
    and every repeat (ADR-0406)."""
    request = ac.request_for_preset(_repeated(run_preset("rear/express", poses=_BEHIND)), candidates=("base", "trial"))
    report = preflight(request, ready_facts(request, candidates={"trial": _cardioid_trial()}))
    assert not report.blocking

    result, _, _ = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": 100.0, "behind": 110.0},
        graph_db={("candidate", "trial", "bearing"): _REAR_SUM_DB, ("candidate", "trial", "behind"): 15.0}))

    behind = [take for take in _takes(result.joined()) if take["pose_kind"] == "behind"]
    probes = [take for take in behind if is_level_probe(ExcitationProgram.from_dict(take["program"]))]
    read = {(take["candidate_id"], take["take_id"]): take["capture_integrity"]["spl"]["max_window_db_spl"]
            for take in behind if take["selected"]}
    assert result.status == "complete" and len(read) == 8
    assert sorted({take["candidate_id"] for take in probes}) == ["banked-base", "trial"] and len(probes) == 2
    assert all(78.0 <= db <= 82.0 for db in read.values()), read


def test_a_branch_set_finds_its_own_level_after_a_summed_set_at_its_spot():
    """A branch take after a close summed take at the same spot never shares the
    summed probe's level, which bounds neither branch alone (ADR-0403 §3)."""
    pose = Pose(0, 0, kind="behind", distance_m=0.1)
    summed, branches = (ac.AngleStop(pose, ac.REGIME_SUMMED, purpose="rear"),
                        ac.AngleStop(pose, ac.REGIME_BRANCHES, purpose="rear", branch_pair="front_rear"))
    request = ac.AngleCaptureRequest(stops=(summed, summed, branches, branches))

    captures = plan_run.prepare_plan_captures(request)

    assert [(capture.spec.graph_scope, capture.spec.level_probe) for capture in captures] == [
        ("candidate", True), ("candidate", False), ("candidate_branches", True), ("candidate_branches", False)]


_OVERRUN = TakeVerdict(False, fault=REASON_CAPTURE_OVERRUN, next="retake_same", charge="speaker")
_UNHEARD = TakeVerdict(False, fault=REASON_SNR_FLOOR, next="fix_and_retake", charge="operator")


def _takes_played_unlevelled(fakes):
    """The plays of a take, not a probe, that asked for no level."""
    return [call for call in fakes.play.calls if call["stimulus_dbfs"] is None
            and not (call["spec"].level_probe and call["spec"].graph_scope != "candidate_branches")]


def test_a_branch_set_whose_second_probe_never_lands_plays_no_more_takes():
    """The rear probe never lands, so the set has one branch's level and no
    take's: its first take is left unmeasured, and the rest of the set plays at
    no level, never at the run's fader (ADR-0361 §3, ADR-0403 §3)."""
    request = ac.request_for_preset(run_preset("rear/pair", "speaker_mark"))

    result, fakes, _, _ = _run_levelled(request, (), ceiling_db=-12.0, chain={"woofer": 110.0, "woofer:rear": 110.0},
                                        verdicts=lambda take: _OVERRUN if take >= 2 else None)

    assert {(call["spec"].graph_scope, call["spec"].branch_target_ids) for call in fakes.play.calls} == {
        ("drivers", ("woofer",)), ("drivers", ("woofer:rear",))}
    assert _takes_played_unlevelled(fakes) == []
    assert [row["reason"] for row in result.not_measured] == [REASON_CAPTURE_OVERRUN, REASON_LEVEL_UNSOLVED]


def test_a_branch_set_whose_first_take_never_lands_carries_both_branches_level():
    """Both probes land, and the take is cut 3 dB for a clip, its new peak the
    front alone at −27 − 3, and then never lands. The rest of the set plays at the
    last level solved for it, the sum and each branch alone, never above the last
    it played (ADR-0403 §3, ADR-0407)."""
    request = ac.request_for_preset(run_preset("rear/pair", "speaker_mark"))
    clipped = TakeVerdict(False, fault=REASON_CLIPPED, next="retake_quieter", charge="speaker", next_gain_db=-30.0)

    retries = MAX_EXTRA_ATTEMPTS_PER_POSITION

    result, fakes, _, _ = _run_levelled(
        request, (), ceiling_db=-12.0, chain=_REAR_UP,
        verdicts=lambda take: clipped if take == 3 else _OVERRUN if 4 <= take <= 3 + retries else None)

    assert fakes.play.stimulus_dbfs == [None, None, -39.0] + [-42.0] * retries + [-42.0]
    assert [call["spec"].branch_levels_dbfs for call in fakes.play.calls] == [
        (), (), (-27.0, -33.0), *[(-30.0, -36.0)] * (retries + 1)]
    assert [row["reason"] for row in result.not_measured] == [REASON_CAPTURE_OVERRUN]


def test_a_close_set_that_finds_no_level_plays_no_more_takes():
    """The behind spot's probe never reads over the room, so its set found no
    level. The rest of the set does not play (ADR-0361 §3, ADR-0403 §3)."""
    request = ac.request_for_preset(_repeated(run_preset("rear/express", "rear_behind")))

    result, fakes, _, _ = _run_levelled(request, (75.0, 79.0, 79.0) + (70.0,) * 8,
                                        verdicts=lambda take: _UNHEARD if take >= 4 else None)

    assert _takes_played_unlevelled(fakes) == []
    assert [row["reason"] for row in result.not_measured] == [REASON_SNR_FLOOR, REASON_LEVEL_UNSOLVED]


@pytest.mark.parametrize("readings, verdicts, asked, reasons", [
    ((66.0,) + (86.0,) * 3 + (80.0,), None,
     [None, -29.0, -36.0, -43.0, -50.0], [REASON_LEVEL_OFF_TARGET]),
    ((60.0,) * 8, lambda take: _UNHEARD, [None] * 3, [REASON_SNR_FLOOR, REASON_LEVEL_UNSOLVED]),
], ids=["solved", "unsolved"])
def test_a_driver_pose_whose_first_take_never_lands_carries_its_level_or_skips(readings, verdicts, asked, reasons):
    """Unlike main, where the next take probes again: a driver's next take at
    the same placement plays at the last level solved for it, with no probe,
    or, when its placement found none, does not play (ADR-0361 §3)."""
    request = ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, repeats=2, kind="close", distance_m=0.015, driver="woofer"),), purposes=("reference",),
        stimulus=NEAR_FIELD))

    result, fakes, _, _ = _run_levelled(request, readings, verdicts=verdicts)

    assert fakes.play.stimulus_dbfs == asked
    assert [row["reason"] for row in result.not_measured] == reasons


@pytest.mark.parametrize("redo_at", [4, 5], ids=["during-the-rear-probe", "after-the-take"])
def test_a_redo_probes_both_branches_again_behind_a_summed_take(redo_at):
    """A summed take at the mark, then a branch set at the mark: a Redo there
    plays the summed take's probe and take again and both probes again before
    the branch take, never the take at no level or at its old one (ADR-0365,
    ADR-0403 §3, ADR-0423)."""
    request = ac.AngleCaptureRequest(stops=(
        ac.AngleStop(Pose(0, 0), ac.REGIME_SUMMED, purpose="rear"),
        ac.AngleStop(Pose(0, 0), ac.REGIME_BRANCHES, purpose="rear", branch_pair="front_rear")))

    result, fakes, _, _ = _run_levelled(request, (), ceiling_db=-12.0, redo_at={redo_at},
                                        chain={"woofer": 110.0, "woofer:rear": 110.0})

    assert result.status == "complete"
    plays = [(call["spec"].graph_scope, call["stimulus_dbfs"]) for call in fakes.play.calls]
    assert plays[redo_at:] == [("candidate", None), ("candidate", -31.0), ("drivers", None), ("drivers", None),
                               ("candidate_branches", -37.0)]


def test_a_redo_as_a_set_finds_no_level_plays_the_rest_at_the_level_it_then_lands():
    """The behind spot's probe is never heard, and the operator presses Redo just
    as that take is left unmeasured. The redone take lands, and the rest of the
    set plays at its level, not skipped as level_unsolved (ADR-0403 §3)."""
    request = ac.request_for_preset(_repeated(run_preset("rear/express", "rear_behind")))

    result, fakes, _, _ = _run_levelled(request, (75.0, 79.0, 79.0) + (70.0,) * 4 + (92.0, 80.0, 80.0),
                                        verdicts=lambda take: _UNHEARD if 4 <= take <= 7 else None,
                                        redo_when_unmeasured=3)

    assert result.status == "complete" and result.not_measured == []
    assert fakes.play.stimulus_dbfs == [None, -38.0, -38.0] + [None] * 5 + [-55.0, -55.0]


def test_a_close_set_whose_first_take_never_lands_plays_on_at_its_last_solved_level():
    """A close set's first take that reads loud at every level is left unmeasured
    once its retakes are spent. The rest of its set plays at the last level solved
    for it, never at the take's ceiling (ADR-0403)."""
    request = ac.request_for_preset(_repeated(run_preset("rear/express", "rear_behind")))

    result, fakes, _, _ = _run_levelled(request, (75.0, 79.0, 79.0, 92.0) + (86.0,) * 3 + (80.0,))

    assert fakes.play.stimulus_dbfs == [None, -38.0, -38.0, None, -55.0, -62.0, -69.0, -76.0]
    assert [row["reason"] for row in result.not_measured] == [REASON_LEVEL_OFF_TARGET]


#: A two-way speaker's drivers, CHECK's plan for them, and the profile a bass take is
#: composed from, as the fake run chain composes its plays.
_RUN_BANDS = {"woofer": FrequencyBand(20, 4000), "tweeter": FrequencyBand(1500, 20000)}
_RUN_GAINS = {"woofer": -20.0, "tweeter": -26.0}
_, _RUN_SAFETY, _RUN_TARGETS = _profile_and_targets(woofer_floor=20)


class _Fader(FakeVolume):
    async def prove(self):
        return self.acquired[-1]


class _RunChain:
    """A fake chain: each stimulus reads its peak at the output plus the chain at
    its spot, over a 35 dB room. A probe reads each burst until the first over
    the 76 dB ramp bound, which stops it (ADR-0365). A scope's gains over the
    level reference graph back its summed takes off, as composition does, and a
    graph (scope, candidate), or that graph at one pose kind, in ``graph_db``
    plays that much louder."""

    def __init__(self, manifest, play, caps, chain_db, scope_gains=None, graph_db=None):
        self.manifest, self.play, self.caps, self.chain_db = manifest, play, caps, chain_db
        self.scope_gains, self.graph_db = scope_gains or {}, graph_db or {}

    async def bank(self, record):
        record = self.manifest.capture_record(record)
        excitation = SessionExcitation(tuple(RoleBand(role, channel, band) for channel, (role, band) in enumerate(
            _RUN_BANDS.items())), self.caps, record["level_db"], 2000.0, dict.fromkeys(self.caps, 8.0),
            target_bands=_RUN_BANDS)
        spec = self.play.calls[-1]["spec"]
        program = program_for_spec(replace(spec, scope_gains_db=self.scope_gains.get(spec.graph_scope)), excitation,
                                   _RUN_GAINS, record.get("stimulus_dbfs"))
        graph = (spec.graph_scope, spec.candidate_id)
        chain = self.chain_db[record["pose_kind"]] + self.graph_db.get(
            (*graph, record["pose_kind"]), self.graph_db.get(graph, 0.0))
        heard = [(segment.gain_db, segment.gain_db + record["level_db"] + chain) for segment in program.stimulus_segments()]
        heard = (heard[:max(1, len(list(takewhile(lambda burst: burst[1] <= 76.0, heard))))] if is_level_probe(program)
                 else [max(heard, key=lambda burst: burst[1])])
        record.update(program=program.to_dict(), levels=[
            (gain, _MIC.dbfs_from_db_spl(level), _MIC.dbfs_from_db_spl(35.0)) for gain, level in heard],
            capture_integrity={"spl": {"max_window_db_spl": heard[-1][1], "loudest_half_second_db_spl": heard[-1][1],
                                       "ceiling_db_spl": 85.0, "sens_factor_db": _MIC.sens_factor_db}})
        return await self.manifest.bank(record)


def _run_chain_analysis(record):
    program = ExcitationProgram.from_dict(record["program"])
    return replace(_measure_analysis(program), stimulus_levels=tuple(LevelReading(*level) for level in record["levels"]))


class _PlacementLog(AnsweredGate):
    """A gate that logs each placement it grants in ``events``."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def gate(self, index, attempt, entry):
        granted = len(self.grants)
        super().gate(index, attempt, entry)
        if len(self.grants) > granted:
            self.events.append("placement")


def _found_door(monkeypatch, windows, events):
    """Patch each level window to log the fader it sets in ``windows`` and ``events``,
    and the household level (None) it leaves when it closes."""
    @asynccontextmanager
    async def window(level_db, *, hold, spl_monitor):
        windows.append(level_db)
        events.append(level_db)
        try:
            yield SimpleNamespace(measurement_volume_db=level_db, spl_monitor=spl_monitor)
        finally:
            events.append(None)

    monkeypatch.setattr(plan_run, "level_window", window)


def _chain_door(fakes, manifest, caps, chain_db, scope_gains=None, graph_db=None):
    chain = _RunChain(manifest, fakes.play, caps, chain_db, scope_gains, graph_db)
    return plan_run.RunDoor(nullcontext(SimpleNamespace()), lambda opened, allocate: TuningSession(
        "run", replace(fakes, records=chain).seams(), opened.measurement_volume_db, allocate),
        _MIC, SimpleNamespace(model_key="minidsp_umik2"), 85.0, caps_dbfs=caps)


def _accept_unlevelled(analysis, **kw):
    return TakeVerdict(True, next="accept") if kw["pose_level"] is None else capture_dispatch.assess(analysis, **kw)


async def _run_found(monkeypatch, request, *, caps, chain_db, gate=None, signals=None, events=None, assessor=None,
                     scope_gains=None, graph_db=None):
    """A run through a door that finds its fader, on a fake chain; each take that
    does not level itself is accepted. Answers the result, the plays and the
    fader of each level window, in order. ``events`` logs each fader a window
    sets and the household level (None) it leaves when it closes."""
    windows: list = []
    _found_door(monkeypatch, windows, [] if events is None else events)
    fakes = FakeSeams(volume=_Fader())
    manifest = RunManifest("run", _Store(fakes.records))
    door = _chain_door(fakes, manifest, caps, chain_db, scope_gains, graph_db)
    result = await plan_run.run_plan(
        request, door=door, manifest=manifest, analyze=_run_chain_analysis, gate=gate or AnsweredGate(),
        aborts=_ABORTS, signals=signals, captures=plan_run.prepare_plan_captures(request),
        assessor=assessor or _accept_unlevelled)
    return result, fakes.play.calls, windows


@pytest.mark.parametrize("tweeter_cap", [-6.0, -20.0], ids=["under the cap", "held by the tweeter cap"])
@pytest.mark.parametrize(("stated", "held"), [(0.0, -9.0), (-15.0, -15.0)])
def test_a_run_probes_its_first_summed_take_before_check_and_holds_the_fader_it_finds(
        monkeypatch, tweeter_cap, stated, held):
    """A speaker run's first play is its timing take's probe, at the loudest
    cap's fader or the level the plan states when lower, from −60 dBFS at the
    output, and it banks as that take's first attempt. The run then holds the
    fader where that take lands 1 dB under 80 dB at the mark, and plays from
    CHECK (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("speaker", "speaker_mark"), level=ac.LevelPolicy(level_db=stated))
    gate = AnsweredGate()

    result, plays, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": tweeter_cap}, chain_db={"bearing": 100.0}, gate=gate))

    probe, *takes = plays
    assert len(gate.grants) == 1
    assert (probe["spec"].graph_scope, probe["spec"].level_probe, probe["level_db"]) == ("timing", True, stated)
    assert [call["spec"].program_phase for call in takes] == ["check", "timing", "measure", "measure"]
    assert windows == [stated, held] and {call["level_db"] for call in takes} == {held}
    timing = sorted((take for take in _takes(result.joined()) if take["phase"] == "timing"), key=lambda take: take["attempt"])
    assert [(take["attempt"], take["selected"]) for take in timing] == [(1, False), (2, True)]
    first = ExcitationProgram.from_dict(timing[0]["program"]).stimulus_segments()[0]
    assert first.effective_peak_dbfs == pytest.approx(-60.0)
    assert timing[1]["capture_integrity"]["spl"]["max_window_db_spl"] == pytest.approx(79.0 + held + 9.0)
    assert {key: result.level["run"][key] for key in ("level_db", "probe_fader_db", "probe_level_db", "source")} == {
        "level_db": held, "probe_fader_db": stated, "probe_level_db": held, "source": "probe"}
    assert result.status == "complete"


@pytest.mark.parametrize(("stated", "held", "source"), [(0.0, -3.0, "probe_fader"), (-10.0, -10.0, "operator")])
def test_a_first_spot_where_every_take_levels_itself_holds_the_probe_fader(monkeypatch, stated, held, source):
    """A run whose first spot is a driver's pose solves no fader: it holds the
    probe fader, the loudest driver cap, never above the level stated, and the
    pose finds its own level from −60 dBFS at the output (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("drivers/each", poses='[{"azimuth_deg": 0, "elevation_deg": 0, '
                                                                      '"kind": "close", "distance_m": 0.015, "driver": "woofer"}]'),
                                    targets=("woofer", "tweeter"), level=ac.LevelPolicy(level_db=stated))

    result, plays, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": -3.0, "tweeter": -6.0}, chain_db={"close": 125.0}))

    assert windows == [held] and [call["stimulus_dbfs"] for call in plays] == [None, -46.0 - held]
    probe = ExcitationProgram.from_dict(min(_takes(result.joined()), key=lambda take: take["attempt"])["program"])
    assert probe.stimulus_segments()[0].effective_peak_dbfs == pytest.approx(-60.0)
    assert (result.level["run"]["level_db"], result.level["run"]["source"]) == (held, source)


@pytest.mark.parametrize(("timing_gain", "fader", "pair"), [(0.0, -9.0, True), (6.0, -3.0, True), (6.0, -3.0, False)],
                         ids=["no scope gain", "a scope gain, a pair that probes its graphs", "a scope gain on one graph"])
def test_the_run_holds_the_fader_its_timing_probe_solves(monkeypatch, timing_gain, fader, pair):
    """A speaker run probes its timing take, which lands 1 dB under 80 dB, and
    holds the fader that probe solves, whatever the timing graph's scope gain:
    nothing cuts it (ADR-0432). A trial's A/B pair probes its own graphs there
    and lands at its own level (ADR-0408)."""
    request = ac.request_for_preset(run_preset("speaker", "speaker_mark"), candidates=("base", "trial") if pair else ())

    result, _, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": 100.0},
        scope_gains={"timing": dict.fromkeys(("woofer", "tweeter"), timing_gain)}))

    assert result.status == "complete" and windows == [0.0, pytest.approx(fader, abs=0.02)]
    read = {(take["phase"], take["candidate_id"]): take["capture_integrity"]["spl"]["max_window_db_spl"]
            for take in _takes(result.joined()) if take["selected"] and take["phase"] in ("timing", "lateral")}
    pairs = {("lateral", "banked-base"), ("lateral", "trial")} if pair else set()
    assert set(read) == {("timing", "banked-base")} | pairs
    assert read == {key: pytest.approx(79.0, abs=0.02) for key in read}


#: jts3's applied bass extension at the smoke test (#6113): a 14.78 dB reserve (ADR-0359).
_JTS3_BASS = {"linkwitz_transform": {"source_hz": 112.8, "source_q": 1.23, "target_hz": 40.0, "target_q": 0.707},
              "delta_highpass_hz": 30.0, "detector_lowpass_hz": 120.0, "compressor_threshold_dbfs": -15.0}


@pytest.mark.parametrize(("program", "layout", "lands_db"), [
    ("speaker", "speaker_mark", 79.0), ("room", "seat_express", 73.0), ("rear/express", "rear_express", 79.0),
    ("room", "room_quick", 79.0)],
    ids=["over a timing take", "at the seats", "at the mark's spots", "on the arm"])
@pytest.mark.parametrize(("box", "over_db"), [
    ("a two-way's room boost", {"banked-base": 6.0, "trial": 6.0}),
    ("a cardioid tune", {"banked-base": 3.32 + _REAR_SUM_DB, "trial": 3.32 + _REAR_SUM_DB}),
    ("jts3's bass and rear seed", {"banked-base": 14.78 + _REAR_SUM_DB, "trial": _REAR_SUM_DB}),
    ("a trial's bass boost", {"banked-base": 0.0, "trial": 14.2})])
def test_each_candidate_graph_levels_its_own_set(monkeypatch, program, layout, lands_db, box, over_db):
    """Each candidate graph's summed takes are one set, however much louder one
    graph plays than the other: its first take probes that graph from −60 dBFS at
    the output and lands 1 dB under its run level, 80 ± 2 dB at the mark or 74 ± 2
    dB at a seat, and its other spots carry that level. So no take of either graph
    plays over the level a run's probed take lands at, and the trial's declared
    bass reserve cuts no fader: a run with a timing take holds the fader that
    take's probe finds, and any other run the probe fader (ADR-0408, ADR-0423, ADR-0432)."""
    selected = run_preset(program, layout)
    request = ac.request_for_preset(selected, candidates=("base", "trial"), mover=selected.mover or ac.MOVER_HUMAN)
    trial = replace(_cardioid_trial(), role_attenuations_db={"woofer": 0.0, "tweeter": -25.2}, bass_extension=_JTS3_BASS)
    assert not preflight(request, ready_facts(request, candidates={"trial": trial})).blocking

    result, _, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -25.0}, chain_db={"bearing": 110.0, "seat": 104.0},
        graph_db={("candidate", name): db for name, db in over_db.items()}))

    takes = _takes(result.joined())
    timing = [take["capture_integrity"]["spl"]["max_window_db_spl"] for take in takes
              if take["selected"] and take["phase"] == "timing"]
    sets = {name: [take for take in takes if take["phase"] == "lateral" and take["candidate_id"] == name]
            for name in over_db}
    probes = {name: [ExcitationProgram.from_dict(take["program"]) for take in rows
                     if is_level_probe(ExcitationProgram.from_dict(take["program"]))] for name, rows in sets.items()}
    kept = {name: [take for take in rows if take["selected"]] for name, rows in sets.items()}
    assert result.status == "complete" and result.to_dict()["honoured"]["retakes"] == 0
    assert windows == ([0.0, windows[-1]] if request.takes_timing else [0.0])
    assert timing == ([pytest.approx(79.0)] if request.takes_timing else [])
    for name, rows in kept.items():
        probe, = probes[name]
        assert probe.stimulus_segments()[0].effective_peak_dbfs == pytest.approx(-60.0)
        assert len(rows) == len(request.stops) // 2 and len({take["level"]["stimulus_dbfs"] for take in rows}) == 1
        assert [take["capture_integrity"]["spl"]["max_window_db_spl"] for take in rows] == pytest.approx(
            [lands_db] * len(rows), abs=0.02), (name, box)


@pytest.mark.parametrize(("chain_db", "assessed", "placements", "reason"), [
    (50.0, None, 1, "level_unreachable"),
    (100.0, TakeVerdict(False, REASON_SNR_FLOOR, next="fix_and_retake", charge="operator"), 3, REASON_SNR_FLOOR),
    (100.0, TakeVerdict(True, next="accept"), 1, REASON_LEVEL_UNSOLVED),
], ids=["buried", "asks-again", "kept"])
def test_a_run_probe_that_finds_no_level_ends_the_run_before_any_take(monkeypatch, chain_db, assessed, placements,
                                                                      reason):
    """A probe the room buries at its ceiling ends the run at once (ADR-0422). One
    that asks for the microphone again does so while its retries last, and the fader
    is back at the household level before each placement, so it sits at the probe
    fader only while the probe plays. A probe an assessor keeps ends the run too,
    since a probe is never kept: nothing plays at a fader no probe found (ADR-0365,
    ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("speaker", "speaker_mark"), level=ac.LevelPolicy(level_db=0.0))
    events: list = []

    result, plays, _ = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": chain_db},
        gate=_PlacementLog(events), events=events,
        assessor=(lambda analysis, **kw: assessed) if assessed else None))

    assert (result.reason, len(plays)) == (reason, placements)
    assert events == ["placement", 0.0, None] * placements
    assert all(call["spec"].level_probe and call["spec"].graph_scope == "timing" for call in plays)
    assert result.level["run"]["level_db"] is None


def test_a_redo_before_the_run_probe_places_the_microphone_for_the_probe_again(monkeypatch):
    """A redo pressed before the run has its fader asks for the placement again,
    and the probe, not CHECK, plays first (ADR-0403 §4)."""
    signals = plan_run.RunSignals()
    request = ac.request_for_preset(run_preset("speaker", "speaker_mark"), level=ac.LevelPolicy(level_db=0.0))

    result, plays, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": 100.0},
        gate=_RedoOnPlacementGate(signals), signals=signals))

    assert plays[0]["spec"].level_probe and windows == [0.0, -9.0] and result.status == "complete"


def test_a_redo_at_a_driver_pose_places_it_again_and_never_ends_the_round():
    """Each redo asks for the microphone again and starts the pose over at its
    probe, with its retries, so redos past the pose's budget never end the
    round; the page is told which plays are the probe, and a pose's takes play
    at the level its probe solved (ADR-0365)."""
    request = ac.request_for_preset(Preset("nearfield/each", tuple(
        Pose(0, 0, repeats=repeats, kind="close", distance_m=mm / 1000, driver="woofer")
        for mm, repeats in ((15, 1), (30, 2))), purposes=("reference",), stimulus=NEAR_FIELD))
    redos = MAX_EXTRA_ATTEMPTS_PER_POSITION + 1
    # The operator presses Redo during each of the first probes, then lets each pose land.
    result, fakes, selected, gate = _run_levelled(request, (66.0,) * (redos + 1) + (80.0, 66.0, 80.0, 80.0),
                                                  redo_at=range(1, redos + 1))

    assert (result.status, result.reason, result.not_measured) == ("complete", "", [])
    assert [index for index, _ in gate.grants] == [1] * (redos + 1) + [2]
    assert fakes.play.stimulus_dbfs == [None] * (redos + 1) + [-29.0, None, -29.0, -29.0]
    assert selected == [False] * (redos + 1) + [True, False, True, True]
    steps = {(p["measurement"], p["attempt"]): p["level_step"] for p in gate.progress if "level_step" in p}
    assert list(steps.values()) == ["probe"] * (redos + 1) + ["levelled", "probe", "levelled", "levelled"]


@pytest.mark.parametrize("repeats,redo_first", [(2, True), (2, False), (6, False)])
def test_a_redo_spends_no_retry_on_the_takes_it_plays_again(monkeypatch, repeats, redo_first):
    """A redo during a pose's last take plays the pose again from its start, and
    each take it plays again is free (#5722): a driver's pose starts over with its
    retries (ADR-0361). The earlier takes stay banked, but neither kept nor the
    level the new placement is held to, so a placement that moved the level is not
    refused as drift. A redo before any take played only asks for the placement again."""
    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    request = ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, repeats=repeats, kind="close", distance_m=0.015, driver="woofer"),),
        purposes=("reference",), stimulus=NEAR_FIELD))
    placement = (66.0, *(80.0,) * repeats)

    result, _, selected, gate = _run_levelled(request, placement * 2, redo_at=(0,) if redo_first else (repeats + 1,))

    assert (result.status, result.reason) == ("complete", "")
    assert [index for index, _ in gate.grants] == [1] * 2
    assert selected == [False] * (len(selected) - repeats) + [True] * repeats
    final = gate.progress[-1]
    assert (final["budget"]["left"], final["retakes"]) == (2, 0)


@pytest.mark.parametrize(("readings", "redo_at", "drifted", "kept", "unmeasured", "left"), [
    ((70.0, 70.0, 70.0, 73.0, 70.0, 70.0), (), {3}, {0, 1, 2, 4, 5}, [], 1),
    ((70.0,) * 4 + (73.0,) * 2, (), {4, 5}, {0, 1, 2, 3}, [REASON_LEVEL_DRIFT_AT_SESSION_GAIN], 1),
    ((70.0,) * 13 + (73.0, 70.0), (5, 10), {13}, {10, 11, 12, 14}, [REASON_LEVEL_DRIFT_AT_SESSION_GAIN], 0),
    ((70.0, 70.0, 70.0, 73.0) + (75.0,) * 5, (4,), {3}, {4, 5, 6, 7, 8}, [], 1),
    ((70.0,) * 15, (5, 10, 15), set(), set(), [REASON_RETRIES_SPENT] * 5, 0),
], ids=["a MEASURE repeat drifts once", "a MEASURE repeat drifts twice to one reading", "no retry left",
        "a redo of a drifted take", "a redo its placement cannot pay for"])
def test_a_take_at_its_runs_fader_is_retaken_for_drift_within_its_placements_cap(
        readings, redo_at, drifted, kept, unmeasured, left):
    """A take at its run's fader never levels itself: a repeat more than SAME_POSE_DRIFT_DB
    off its placement's kept takes is retaken at that level, each retake one of the
    placement's two extra takes (ADR-0422), and banks the verdict and level it was judged
    by (ADR-0383); a second drift to the same reading spends the placement (ADR-0428).
    A redo spends one, the takes it plays again none, and its new placement's level is
    no drift (#5722)."""
    request = ac.AngleCaptureRequest((ac.AngleStop(Pose(0, 0), ac.REGIME_PER_DRIVER, purpose="speaker"),) * 3,
                                     program="speaker/mark")

    result, fakes, selected, gate = _run_levelled(request, readings, redo_at=redo_at)

    rows, records = sorted(result.takes, key=lambda row: row["take_id"]), {r["take_id"]: r for r in fakes.banked}
    assert [row["reason"] for row in result.not_measured] == unmeasured
    assert selected == [take in kept for take in range(len(rows))]
    assert [row.get("fault") for row in rows] == [REASON_LEVEL_DRIFT_AT_SESSION_GAIN if take in drifted else None
                                                  for take in range(len(rows))]
    assert {row["level"]["level_delta_db"] for take, row in enumerate(rows) if take in drifted} <= {3.0}
    for row in rows:
        record = records[row["take_id"]]
        assert record["level"] == {**row["level"], "alignment": row["alignment"]}
        assert (record["verdict"]["fault"], record["verdict"]["next"]) == (row.get("fault"), row.get("next", "accept"))
    assert gate.progress[-1]["budget"]["left"] == left


@pytest.mark.parametrize("purpose,layout,entry,poses", [
    ("speaker", "baseline_express", True, 6), ("room", "seat_express", False, 3),
    ("rear/express", "rear_express", False, 3),
])
def test_program_timing_take_and_placement_count(purpose, layout, entry, poses):
    request = ac.request_for_preset(run_preset(purpose, layout))
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    captures = plan_run.prepare_plan_captures(request, roles_bands=context.roles_bands)
    assert any(c.spec.program_phase == "timing" for c in captures) is entry
    assert plan_run.preview_schedule(request, captures, context)["poses"] == poses


async def test_a_run_banks_its_preset_and_its_layout():
    """The banked record names the preset and, in its own field, the layout it walked (ADR-0366 §6)."""
    result, _ = await _run_gated(ac.request_for_preset(run_preset("tournament", "tournament_full")))
    assert {key: result.to_dict()[key] for key in ("preset", "layout")} == {
        "preset": "tournament/express", "layout": "tournament_full"}


@pytest.mark.parametrize("name,layout,banked", [
    ("speaker/mark", "speaker_mark", {("speaker", ("speaker",))}),
    ("rear/seat", "seat_express", {("rear", ("rear", "room", "bass"))}),
])
async def test_a_take_banks_the_purposes_its_stop_names(name, layout, banked):
    """A preset names its purposes on each stop, and each take banks them (ADR-0336, ADR-0383)."""
    request = ac.request_for_preset(run_preset(name, layout))
    result, fakes = await _run_gated(request, captures=plan_run.prepare_plan_captures(request),
                                     assessor=lambda *_args, **_kwargs: TakeVerdict(True, next="accept"))
    assert result.status == "complete"
    assert {(take["measurement_purpose"], tuple(take["purposes"])) for take in fakes.banked} == banked


@pytest.mark.parametrize("purpose,purposes", [("room", ("rear", "room")), ("rear", ("rear", "rear"))])
def test_a_stop_names_its_purpose_first_and_each_purpose_once(purpose, purposes):
    pose = replace(run_preset("rear/seat", "seat_express").poses[0], repeats=1)
    with pytest.raises(ac.CrossoverV2FlowError):
        ac.AngleStop(pose, ac.REGIME_SUMMED, purpose=purpose, purposes=purposes)


async def test_room_uses_its_first_seat_take_as_the_level_reference():
    request = ac.request_for_preset(run_preset("room", "seat_express"))
    captures = plan_run.prepare_plan_captures(request)
    manifest = RunManifest("run", _Store(FakeSeams().records), preset=request.program)
    for index, (capture, observed, accepted, action, delta) in enumerate(zip(
        captures, (70, 78), (True, False), ("accept", "retake_same"), (None, 8),
    ), 1):
        assert (capture.spec.program_phase, capture.stop.pose.kind) == ("lateral", "seat")
        manifest.begin({"index": index, "pose": {"kind": "seat", "seat_offset_m": capture.stop.pose.seat_offset_m},
                        "candidate_id": capture.stop.candidate_id}, attempt=1, pose_index=index - 1)
        record = {"take_id": str(index), "level_db": -20, "stimulus_id": "room", "phase": "lateral",
                  "capture_integrity": {"spl": {"loudest_half_second_db_spl": observed}}}
        verdict = capture_dispatch.level_drift_verdict(**manifest.level_observation(record))
        assert (verdict.ok, verdict.next, verdict.evidence.get("level_delta_db")) == (accepted, action, delta)
        await manifest.append(record, str(index), verdict, complete=True, level_observation=verdict.evidence)
    assert [take["selected"] for take in _takes(manifest.to_dict())] == [True, False]


@pytest.mark.parametrize(("requested", "level", "source"), [(-25, -25, "operator"), (0, 0, "operator")])
async def test_check_plays_at_the_session_level(tmp_path, box, requested, level, source):
    from tests.test_correction_crossover_v2_wired import _run_door

    fakes = FakeSeams()
    manifest = RunManifest("run", _Store(fakes.records))
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(Pose(0, 0), ac.REGIME_PER_DRIVER, purpose="speaker"),),
        level=ac.LevelPolicy(level_db=requested), level_source=source, program="speaker/mark",
    )
    door = _run_door(tmp_path, box, fakes, manifest)
    door.build_session = Mock(wraps=door.build_session)

    async def measure(session, spec):
        assert box.volume_db == level
        return await session.measure(spec)

    result = await plan_run.run_plan(
        request, door=door, manifest=manifest, analyze=_analysis,
        aborts=_ABORTS, measure=measure,
        captures=plan_run.prepare_plan_captures(request),
        assessor=lambda *_args, **_kwargs: TakeVerdict(True),
    )
    door.build_session.assert_called_once()
    assert result.status == "complete"
    assert manifest.to_dict()["level"] == {"run": {"level_db": level, "level_source": source}}
    assert {row["capture_basis"]["level_db"] for row in manifest.to_dict()["sets"]} == {level}
    assert [(call["spec"].program_phase, call["level_db"]) for call in fakes.play.calls] == [
        (phase, level) for phase in ("check", "timing", "measure")]


async def test_manifest_discloses_program_default_when_the_plan_states_no_level():
    result, _ = await _run_gated(_walk([0]))

    assert result.to_dict()["level"]["run"] == {"level_db": -20.0, "level_source": "program_default"}


@pytest.mark.parametrize("level", [-20, -25])
async def test_run_requires_the_chosen_level_in_an_open_session(level):
    request = replace(_walk([0]), level=ac.LevelPolicy(level_db=level))
    if level == -25:
        with pytest.raises(ac.LateralWalkRefused) as refused:
            await _run_gated(request)
        assert refused.value.reason == ac.WALK_LEVEL_POLICY_INVALID
    else:
        result, _ = await _run_gated(request)
        assert result.status == "complete"


@pytest.mark.parametrize("purpose", ["room", "speaker"])
async def test_pilot_floor_keeps_take_and_packet_evidence(tmp_path, purpose):
    program = _phase_program(_conductor(FlowSeams()), "verify")
    analysis = _verify_analysis(program, pilot_snr_ok=False, pilot_hi_dbfs=-65, linearity=None)
    analysis = replace(analysis, pilots=(replace(analysis.pilots[0], snr_valid=False, snr_db=0.0),),
                       ambient_report={"bands": [{"band_hz": [500, 2000], "level_dbfs": -65}]})
    verdict = capture_dispatch.assess(analysis, phase="verify", program=program)
    assert verdict.ok is False
    assert verdict.fault == "pilot_level_collapse"
    request = _walk([0])
    request = replace(request, stops=(replace(request.stops[0], purpose=purpose, purposes=(purpose,)),))
    result, _ = await _run_gated(request, analyze=lambda *_args: analysis)
    assert result.status == "partial"
    screens = _takes(result.joined())[0]["verdict"]["screens"]
    assert screens[0]["blocking"] is True
    assert screens[0]["evidence"]["pilot_snr_ok"] is False
    assert screens[0]["evidence"]["pilots"][0]["level_hi_dbfs"] == -65

    manifest = RunManifest("pilot", _Store(FakeSeams().records), preset=purpose)
    manifest.begin({"index": 1, "pose": {"kind": "bearing", "azimuth_deg": 0}}, attempt=1, pose_index=0)
    await manifest.append({"take_id": "pilot", "program": program.to_dict()}, "record", verdict,
                          complete=True, level_observation={})
    root = await asyncio.to_thread(bank_seat_round, tmp_path / "round")
    take_artifact_path(round_inputs(root).session_dir, "record").write_text(json.dumps(
        {"take_id": "pilot", "curves": [], "verdict": asdict(verdict)}))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest.to_dict()))
    packet = write_round_packet(root, str(path), [])
    screen = packet["sets"][0]["takes"][0]["screens"][0]
    assert screen == verdict.screens[0]
    assert (screen["code"], screen["blocking"]) == ("pilot_level_collapse", True)
    evidence = screen["evidence"]
    pilot = evidence["pilots"][0]
    band = next(segment for segment in program.segments if segment.kind == "pilot")
    assert pilot["band_hz"] == [band.f1_hz, band.f2_hz]
    assert (pilot["level_lo_dbfs"], pilot["level_hi_dbfs"], pilot["snr_db"]) == (-75, -65, 0)
    assert evidence["required_snr_db"] > pilot["snr_db"]
    assert evidence["ambient_report"] == analysis.ambient_report


async def test_run_door_preemption_defers_volume_restore_and_restores_graph(tmp_path, box):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    fakes = FakeSeams()
    manifest = RunManifest("run", _Store(fakes.records))
    door = _run_door(tmp_path, box, fakes, manifest)
    request = replace(_walk([0, 20, 40]), level=ac.LevelPolicy(level_db=-20))
    owner, preemptor = volume_owner(), None

    async def measure(session, spec):
        nonlocal preemptor
        outcome = await session.measure(spec)
        if preemptor is None:
            preemptor = await owner.acquire_level(ClaimKind.COMMISSIONING, -30)
        return outcome

    try:
        result = await plan_run.run_plan(
            request, door=door, manifest=manifest, analyze=_analysis,
            aborts=_ABORTS, measure=measure, captures=_summed_captures(request),
        )
        assert (result.reason, result.status, result.takes_measured) == ("internal_error", "partial", 1)
        assert fakes.play.bearings == [0, 20]
        assert door.isolation.restore_result is SessionVolumeRestoreResult.DEFERRED
        assert result.finalized and not door.is_open
        assert fakes.graph.restores == 1
    finally:
        if preemptor is not None:
            await owner.release(preemptor)


async def test_manifest_stamps_watch_levels_and_uses_accepted_medians():
    manifest = RunManifest("run", _Store(FakeSeams().records), level={"run": {"level_db": -15.0}})
    cases = [(0, -15, "a", "", 70, True, None), (0, -15, "a", "", 72, True, 2), (0, -15, "a", "", 90, False, 19),
             (20, -15, "a", "", 76, True, 5), (0, -15, "a", "", 72, True, 1), (0, -25, "a", "", 60, True, None),
             (0, -15, "b", "", 55, True, None), (0, -15, "b", "", 58, True, 3), (0, -15, "a", "", 73, True, 1),
             (0, -15, "a", "fp-cut", 64, True, None), (0, -15, "a", "fp-cut", 65, True, 1)]
    for index, (pose, gain, program, candidate, observed, accepted, delta) in enumerate(cases):
        manifest.begin({"index": index, "pose": {"kind": "bearing", "azimuth_deg": pose}, "candidate_id": candidate},
                       attempt=1, pose_index=index)
        record = {"take_id": str(index), "level_db": -99, "provenance": {"session_volume_db": gain}, "phase": "measure", "stimulus_id": program,
                  "capture_integrity": {"spl": {"loudest_half_second_db_spl": observed, "max_window_db_spl": 99}}}
        await manifest.append(record, str(index), TakeVerdict(accepted), complete=True,
                              level_observation=plan_run.level_drift_verdict(**manifest.level_observation(record)).evidence)
        row = next(take for take in manifest.takes if take["take_id"] == str(index))
        assert row["level"]["loudest_half_second_db_spl"] == observed
        assert row["level"]["level_delta_db"] == delta
    assert manifest.to_dict()["level"] == {"run": {"level_db": -15.0}}


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("trial", [0, 8, 9])
def test_schedule_sweeps_repeats_and_retry_progress(monkeypatch, retry, trial):
    roles = ["summed", "woofer", "tweeter", "woofer", "tweeter", "woofer", "tweeter"]
    roles = ["summed"] * 3 if trial else roles
    segments = tuple(SimpleNamespace(role=role, kind="pilot" if trial and n != 1 else "sweep", start_sample=0, n_samples=4)
                     for n, role in enumerate(roles))
    request = (_walk([0], ("fp-a", "fp-b", "fp-c", "fp-d"), repeats=2) if trial == 8 else
               _walk([0, -20, 20], ("fp-a", "fp-b", "fp-c")) if trial == 9 else _walk([0, -20, 20]))
    counts = [8] if trial == 8 else [3, 3, 3] if trial == 9 else [1, 1, 1]
    captures = plan_run.prepare_plan_captures(request)
    program = SimpleNamespace(phase="measure", sample_rate_hz=1, segments=(), stimulus_segments=lambda: segments,
                              total_samples=4 * len(roles))
    if trial:
        context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                                  driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
        preview = plan_run.preview_schedule(request, captures, context)
        assert (preview["measurements"], preview["measurements_per_pose"], preview["sweeps"]) == (trial, counts, trial * 3)
    gate = AnsweredGate()
    original = FakePlay.run
    async def play(self, **kwargs):
        async def emitted():
            done = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_later(0, done.set_result, None)
            await done
            return await original(self, **kwargs)
        return await plan_run.playback_observer.get()(program, emitted)
    monkeypatch.setattr(FakePlay, "run", play)
    verdicts = iter(([TakeVerdict(False, "snr_floor", next="retake_louder", next_gain_db=-12, charge="speaker")]
                     if retry else []) + [TakeVerdict(True)] * len(captures))
    monkeypatch.setattr(plan_run, "assess", lambda *a, **kw: next(verdicts))
    door = plan_run.RunDoor(AsyncExitStack(), lambda *a: None, None, None, 90, program_for_spec=lambda spec: program)
    result, _ = asyncio.run(_run_gated(request, gate=gate, door=door))
    live = [p for p in gate.progress if p.get("role")]
    assert result.status == "complete"
    if not trial:
        assert [(p["sweep"], p["role"], p["repeat"], p["repeats"]) for p in live[:7]] == [
            (1, "summed", 1, 1), (2, "woofer", 1, 3), (3, "tweeter", 1, 3),
            (4, "woofer", 2, 3), (5, "tweeter", 2, 3), (6, "woofer", 3, 3), (7, "tweeter", 3, 3)]
    assert live[0]["measurements_per_pose"] == counts
    assert live[0]["measurements"] == sum(counts)
    assert live[0]["sweeps_per_pose"] == [n * len(roles) for n in counts]
    assert live[0]["estimated_seconds"] == (sum(counts) * (len(roles) * 4 + WIRED_POST_ROLL_S)
                                            + len(counts) * plan_run.HUMAN_MOVE_ALLOWANCE_S)
    assert {p["pose"] for p in live} == set(range(1, len(counts) + 1))
    assert live[-1]["measurement"] == sum(counts)
    assert {p["measurement"] for p in live} == set(range(1, sum(counts) + 1))
    assert gate.progress[-1]["takes"] == sum(counts)
    assert gate.progress[-1]["retakes"] == int(retry)
    if retry:
        retried = [p for p in live if p.get("retake_reason")]
        assert {p["retake_measurement"] for p in retried} == {1}
        assert {p["measurement"] for p in retried} == {1}
        assert all(p["level_raise_dbfs"] == -12 for p in retried)
    else:
        assert all("retake_reason" not in p for p in live)


def test_a_pose_that_levels_itself_is_timed_as_its_probes_and_its_takes():
    """A driver's pose plays its whole level probe once before its takes, and a
    branch set's first take one probe of each branch; a far-field pose plays no
    probe (ADR-0365, ADR-0403 §3). Each play counts its whole program and the
    recorder's post-roll."""
    band = RoleBand("woofer", 0, FrequencyBand(20, 2000))
    take = build_measure_program({"woofer": -20.0}, (band,), repeat_count=1, sweep_durations={"woofer": 0.2})
    probe = build_level_probe_program(band, (-40.0, -34.0), sweep_band_hz=(20.0, 2000.0), gap_s=0.5,
                                      downstream_gain_db=0.0, channels=1)
    driver = MeasureSpec(kind="candidate", branch_target_ids=("woofer",), program_phase="lateral", level_probe=True)
    branch = MeasureSpec(kind="candidate", graph_scope="candidate_branches", candidate_id="fp-a",
                         branch_target_ids=("woofer", "woofer:rear"), program_phase="lateral", level_probe=True)
    captures = ([({"place": "at_driver", "driver": "woofer"}, driver),
                 ({"place": "at_driver", "driver": "woofer"}, replace(driver, level_probe=False))]
                + [({"place": "far"}, MeasureSpec(kind="candidate", program_phase="lateral"))]
                + [({"place": "mark"}, branch), ({"place": "mark"}, replace(branch, level_probe=False))])

    facts = plan_run.schedule_facts(
        captures, lambda spec, stimulus_dbfs=None: (
            probe if spec.level_probe and spec.graph_scope == "drivers" and stimulus_dbfs is None else take),
        mover="arm")

    take_s, probe_s = (program.total_samples / program.sample_rate_hz + WIRED_POST_ROLL_S for program in (take, probe))
    assert facts["estimated_seconds"] == pytest.approx(5 * take_s + 3 * probe_s)


def test_the_preview_times_every_play_whole_and_announces_the_run_once():
    """The page's estimate is every play's whole composed program, silences
    included, and the recorder's post-roll after it: the run's probe and each
    take, the first take carrying the courtesy prelude that announces the run
    once (ADR-0417), and a person's move to each spot."""
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    row = run_preset("room", "seat_express")
    request = ac.request_for_preset(row)
    captures = plan_run.prepare_plan_captures(request, roles_bands=context.roles_bands)
    compose = predictive_program_for_spec(context)
    first, *rest = (capture.spec for capture in captures)
    plays = [compose(replace(first, level_probe=True)), compose(replace(first, level_probe=False, courtesy_prelude=True)),
             *map(compose, rest)]

    facts = plan_run.preview_schedule(request, captures, context)

    assert facts["estimated_seconds"] == pytest.approx(
        sum(play.total_samples / play.sample_rate_hz + WIRED_POST_ROLL_S for play in plays)
        + len(row.poses) * plan_run.HUMAN_MOVE_ALLOWANCE_S)


@pytest.mark.parametrize("repeats, counts, preparation", [(1, [15, 8, 8], 12), (2, [23, 16, 16], 18)])
def test_three_pose_preview_counts_preparation_and_timing(repeats, counts, preparation):
    """A pose's repeats repeat its takes; the run's timing take plays once."""
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    request = ac.request_for_preset(_repeated(run_preset("tournament", "tournament_full"), repeats))
    captures = plan_run.prepare_plan_captures(request, roles_bands=context.roles_bands)
    facts = plan_run.preview_schedule(request, captures, context)
    assert facts["measurements"] == len(captures)
    assert sum(facts["measurements_per_pose"]) == len(captures)
    assert facts["sweeps_per_pose"] == counts
    assert facts["timing_sweeps"] == 1
    assert facts["preparation_sweeps"] == preparation
    assert facts["sweeps"] == sum(counts)


@pytest.mark.parametrize("stated,played_at_one,repeats_at_one", [
    ("preset", [["woofer", "tweeter"]] * 2, 2),
    ("pose", [["woofer", "tweeter"]] * 2, 2),
    ("driver_pose", [["woofer"], ["tweeter"]], 1),
], ids=["preset", "pose", "driver_pose"])
def test_a_take_plays_previews_and_prices_the_sweeps_its_preset_or_pose_states(stated, played_at_one, repeats_at_one):
    """A preset row, or a pose of its layout, states how many sweeps each driver
    plays in one take: each driver of a MEASURE take, or the one driver of a
    driver's pose. The composer, the page's preview and the dry run's price read
    that one count (ADR-0434)."""
    roles = tuple(_roles())
    context = SimpleNamespace(roles_bands=roles, driver_caps_dbfs={r.role: 0.0 for r in roles}, fc_hz=2500,
                              driver_sweep_duration_limits_s={r.role: 6.0 for r in roles},
                              driver_bands={r.role: r.band for r in roles}, safety_profile={}, role_targets={})
    compose = predictive_program_for_spec(context)

    def planned(row):
        plan = ac.request_for_preset(row)
        captures = plan_run.prepare_plan_captures(plan, roles_bands=roles)
        swept = [index for index, c in enumerate(captures)
                 if c.spec.graph_scope == "drivers" and c.spec.program_phase != "check"]
        played = [[s.role for s in compose(replace(captures[index].spec, level_probe=False)).stimulus_segments()
                   if s.kind == "sweep"] for index in swept]
        facts = plan_run.preview_schedule(plan, captures, context)
        previewed = [(r["role"], r["repeats"]) for pose in facts["pose_sweeps"] for r in pose if r["kind"] == "sweep"]
        price = preflight(plan, ready_facts(plan, context=context, roles_bands=roles)).price
        assert price == {"captures": len(captures), "mic_moves": 1, "seconds": round(facts["estimated_seconds"])}
        return played, previewed, price

    # speaker/mark: CHECK, the timing take, then MEASURE twice at the mark;
    # drivers/each: each driver alone at the mark.
    default = run_preset("drivers/each" if stated == "driver_pose" else "speaker/mark")
    one = {"preset": replace(default, sweeps_per_take=1),
           "pose": run_preset("speaker/mark", poses=[
               {"azimuth_deg": 0, "elevation_deg": 0, "repeats": 2, "sweeps_per_take": 1}]),
           "driver_pose": replace(default, poses=tuple(replace(p, sweeps_per_take=1) for p in default.poses))}[stated]
    (played, previewed, price), (played_3, previewed_3, price_3) = planned(one), planned(default)

    assert (played, played_3) == (played_at_one, [take * 3 for take in played_at_one])
    assert previewed == [(role, repeats_at_one) for take in played for role in take]
    assert previewed_3 == [(role, 3 * repeats_at_one) for take in played_3 for role in take]
    assert price["captures"] == price_3["captures"] and price["seconds"] < price_3["seconds"]


@pytest.mark.parametrize("muted", [False, True], ids=["over_limits", "output_muted"])
@pytest.mark.parametrize("site", ["transaction", "executor"])
async def test_run_host_banks_admission_failure_code_and_segments(monkeypatch, tmp_path, box, site, muted):
    """A refused admission banks its code; one that found an excited output in its
    terminal mute banks that output's own code (#6113)."""
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    admission = ProgramAdmission("verify", "verify", -23, (), (), (ProgramAdmissionRefusal.GRAPH_NOT_PROVEN,),
                                 {"target_id": "woofer:rear", "output_index": 2}) if muted else ProgramAdmission(
        "verify", "verify", -23, (SegmentAdmission("summed-1", "summed", 0, (20, 20000), -23, False, ()),), (),
        (ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS,))
    reason = "program_output_muted" if muted else "program_admission_refused"
    failure = ProgramPlaybackRefused(admission)
    fakes = FakeSeams()
    store = _Store(fakes.records)
    manifest = RunManifest("run", store)
    if site == "transaction":
        play = AsyncMock()
        prepared = ProgramForStimulus(SimpleNamespace(stimulus_id="verify", phase="verify"), {
            "readmit": AsyncMock(return_value=admission), "play_wav": play, "writer_lock": Mock(),
        })
        monkeypatch.setattr(fakes.play, "run", ProgramPlaybackTransaction(
            compose=lambda **kw: prepared, session_volume_plan=SimpleNamespace(assert_ready=Mock()),
        ).run)
    else:
        monkeypatch.setattr(fakes.play, "run", AsyncMock(side_effect=failure))
    door = _run_door(tmp_path, box, fakes, manifest)
    request = replace(_walk([0, 20]), level=ac.LevelPolicy(level_db=-20))
    run = plan_run.run_plan(request, door=door, manifest=manifest, analyze=_analysis, gate=AnsweredGate(),
                            aborts=_ABORTS, captures=_summed_captures(request))
    if site == "executor":
        with pytest.raises(ProgramPlaybackRefused):
            await run
    else:
        await run
        play.assert_not_awaited()
    assert [row["reason"] for row in manifest.not_measured] == [reason, REASON_NOT_REACHED]
    saved = store.snapshots[-1]
    assert saved["reason"] == reason
    if site == "transaction":
        never_played, = (take for group in saved["sets"] for take in group["takes"])
        assert never_played == {"take_id": never_played["take_id"], "record_id": "", "selected": False}
        assert with_records(tmp_path, saved, every_take=True)["sets"] == saved["sets"]
        stop = saved["not_measured"][0]
        assert (stop["fault"], stop["evidence"]["admission"]) == (reason, admission.to_dict())
