# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The executor's observable work, placement, evidence and control contract."""

from __future__ import annotations

import asyncio
import json
import math
from copy import deepcopy
from itertools import count, groupby, takewhile
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from functools import partial
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

from jasper.active_speaker.capture_schedule import walk_price
from jasper.active_speaker import angle_capture as ac, plan_run
from jasper.active_speaker.excitation_safety_plan import resolve_driver_excitation_ceilings
from jasper.active_speaker.run_levels import (
    LEVEL_OFFSETS_DB, LevelRun, ladder_captures, level_ladder, preflight_levels, prepare_level_captures, run_levels,
)
from jasper.active_speaker.measurement_programs import (
    PROGRAM_ROWS, Pose, Preset, preset, run_preset, run_purpose,
)
from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2 import capture_dispatch
from jasper.active_speaker.crossover_v2.programs import SessionExcitation, program_for_spec
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.round_inputs import SetTakes, round_inputs, with_records
from jasper.active_speaker.crossover_v2.round_views.directivity import _pose as directivity_pose
from jasper.active_speaker.crossover_v2.admission import MAX_AUTOMATIC_RETAKES_PER_POSITION, MAX_EXTRA_ATTEMPTS_PER_POSITION
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_CANDIDATE
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec, branch_probes
from jasper.active_speaker.crossover_v2.position_gate import POSITION_HOLD_EXPIRED_CODE, PositionGate
from jasper.active_speaker.crossover_v2.room_selection import purpose_take_records
from jasper.active_speaker.crossover_v2.session import TuningSession
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY, REASON_DRIFT_BASELINES_DISAGREE, REASON_CLIPPED, REASON_ANCHOR_AMBIGUOUS, REASON_CHANNEL_MAP_MISMATCH,
    REASON_SPL_CEILING_EXCEEDED, REASON_LEVEL_DRIFT_AT_SESSION_GAIN, REASON_LEVEL_OFF_TARGET, REASON_RETRIES_SPENT,
    REASON_INTERNAL_ERROR, REASON_CAPTURE_OVERRUN, REASON_LEVEL_UNSOLVED, REASON_SNR_FLOOR, TakeVerdict,
)
from jasper.active_speaker.program_admission import ProgramAdmission, ProgramAdmissionRefusal, SegmentAdmission
from jasper.active_speaker.program_playback import ProgramPlaybackRefused
from jasper.active_speaker.crossover_v2.playback_transaction import PlaybackInterrupted
from jasper.active_speaker.crossover_v2.program_transaction import ProgramForStimulus, ProgramPlaybackTransaction
from jasper.active_speaker.run_manifest import (
    RunManifest, RUN_MANIFEST_KIND, TAKE_INCOMPLETE, TAKE_MEASURED, kept_measurements, view_sets,
)
from jasper.active_speaker.round_packet import RoundPacket, write_round_packet
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
from jasper.audio_resources.volume_owner import ClaimKind, volume_owner
from jasper.platform.json_fields import CodedFieldError
from jasper.web import correction_run_host
from tests.program_baseline_fixtures import banked_program_baselines  # noqa: F401
from tests.crossover_v2_fixtures import (
    FakeSeams as FlowSeams, _conductor, _loc, _measure_analysis, _verify_analysis, _roles,
)
from tests.crossover_v2_banked_round import bank_seat_round
from tests.engine_twin import FakeGraph, FakeSeams, FakePlay, FakeVolume, SeamFailure, open_session
from tests._log_events import event_fields
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_preflight import (
    _REAR_SUM_DB, _cardioid_trial, _unprobed_plans, ready_facts,
)
from tests.test_active_speaker_measurement_door import box as box  # noqa: F401
from tests.test_crossover_v2_tuning_scope import _room_candidate, tuning_profile as tuning_profile

_ABORTS = {SeamFailure: "seam_failed"}

def _walk(angles, candidates=("fp-a",)):
    return ac.AngleCaptureRequest(candidates=candidates, stops=tuple(
        ac.AngleStop(Pose(angle, 0), ac.REGIME_SUMMED, candidate_id=candidate, purpose="speaker")
        for angle in angles for candidate in candidates),
        template=ac.walk_template(kind=MEASURE_KIND_CANDIDATE), program="tournament/express")


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
    rows = [(stop, repeat) for stop in request.stops for repeat in range(1, request.repeats + 1)]
    return tuple(plan_run.PlanCapture(stop, spec, repeat) for (stop, repeat), spec in zip(rows, specs) if spec is not None)


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


@pytest.mark.parametrize("reader,refusal,field,code", [
    ("staged_stop", ac.LateralWalkRefused, "reason", ac.WALK_STOP_NO_LONGER_VALID),
    ("kept_take", CodedFieldError, "code", "field_required"),
    ("purpose_take", CodedFieldError, "code", "field_required"),
])
def test_a_stop_or_take_that_names_no_purpose_refuses_by_its_code(tmp_path, reader, refusal, field, code):
    """No purpose is inferred from a pose kind (#2902): a staged stop that
    names none is no longer valid, and a banked take that names none refuses
    by that field."""
    plan = ac.request_for_preset(run_preset("room", "seat_cube")).to_dict()
    del plan["stops"][0]["purpose"]
    root = bank_seat_round(tmp_path)
    session, = (root / "bundle").iterdir()
    take = next(session.rglob("positions/*.json"))
    take.write_text(json.dumps({key: value for key, value in json.loads(take.read_text()).items()
                                if key != "measurement_purpose"}))
    reads = {
        "staged_stop": lambda: ac.AngleCaptureRequest.from_mapping(plan),
        "kept_take": lambda: list(kept_measurements(session, phases=("lateral",), purposes=("room",))),
        "purpose_take": lambda: purpose_take_records(session, purpose="room"),
    }
    with pytest.raises(refusal) as refused:
        reads[reader]()
    assert getattr(refused.value, field) == code


@pytest.mark.parametrize(("angles", "candidates"), [([0], ("fp-a",)), ([0, 20], ("fp-a", "fp-b")), ([0, -20, 20], ("fp-a",))])
@pytest.mark.parametrize("repeats", [1, 2, 3])
def test_a_walk_groups_configs_and_repeats_under_one_pose_grant(angles, candidates, repeats):
    request, gate = replace(_walk(angles, candidates), repeats=repeats), AnsweredGate()
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
        assert [(t["pose"]["azimuth_deg"], t["repeat"], t["selected"]) for t in group["takes"]] == [
            (angle, repeat, True) for angle in angles for repeat in range(1, repeats + 1)]
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
    assert fakes.play.rungs == ([None, gain] if refusal else [None])
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
@pytest.mark.parametrize("budget", [0, 3])
def test_pose_budget_counts_retries_and_bounds_automatic_work(monkeypatch, charge, budget):
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: TakeVerdict(False,
        REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge=charge))
    gate = AnsweredGate()
    result, fakes = asyncio.run(_run_gated(replace(_walk([0]), retries_per_pose=budget), gate=gate))
    assert result.status == "partial"
    assert len(fakes.banked) == 1 + (MAX_AUTOMATIC_RETAKES_PER_POSITION if charge == "speaker" else budget)
    assert gate.progress[-1]["budget"]["by_household"] == (0 if charge == "speaker" else budget)
    assert gate.progress[-1]["budget"]["left"] == 0
    assert result.reason == ""
    assert result.not_measured[0]["reason"] == REASON_DRIFT_BASELINES_DISAGREE


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
            *(TakeVerdict(False, reason, next="fix_and_retake", charge="operator") for _ in range(4)),
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
    assert fakes.play.bearings == ([0, 20, 20, 20, 20, 40] if quality_refusal else [0, 20])
    assert [stop["index"] for stop in result.not_measured] == ([2] if quality_refusal else [2, 3])
    assert all(stop["reason"] == reason for stop in result.not_measured)


def test_exhausted_clipped_stop_ends_the_run(monkeypatch):
    verdicts = iter([
        TakeVerdict(True),
        *(TakeVerdict(False, REASON_CLIPPED, next="retake_quieter", next_gain_db=-24,
                      charge="speaker") for _ in range(7)),
    ])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))

    result, fakes = asyncio.run(_run_gated(_walk([0, 20, 40])))

    assert result.reason == REASON_CLIPPED
    assert fakes.play.bearings == [0, *([20] * 7)]
    assert [stop["index"] for stop in result.not_measured] == [2, 3]


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
        assert len(result.not_measured) == 1


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


@pytest.mark.parametrize(("stage", "action", "budget", "retried"), [
    ("restore", "stop", 1, False), ("restore", "accept", 1, False),
    ("restore", "retake_same", 1, True), ("restore", "retake_louder", 1, True),
    ("restore", "retake_quieter", 1, True), ("restore", "retake_same", 0, False),
    ("ready", "stop", 1, False),
])
def test_incomplete_take_obeys_verdict_and_accounts_for_remaining_stops(monkeypatch, stage, action, budget, retried):
    verdicts = iter([TakeVerdict(False, REASON_CLIPPED, next=action, charge="operator",
                               next_gain_db=-15 if action == "retake_louder" else -24),
                     TakeVerdict(True), TakeVerdict(True)])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    seams = FakeSeams(play=FakePlay(script=[(stage, REASON_CLIPPED)]))
    result, _ = asyncio.run(_run_gated(replace(_walk([0, 20]), retries_per_pose=budget), seams=seams))
    assert result.status == ("complete" if retried else "partial")
    assert seams.play.bearings == ([0, 0, 20] if retried else [0])
    assert result.takes[0]["quality"]["status"] == TAKE_INCOMPLETE
    assert result.takes[0]["fault"] == REASON_CLIPPED
    assert result.takes[0]["next"] == ("stop" if action == "accept" else action)
    assert [stop["index"] for stop in result.not_measured] == ([] if retried else [1, 2])
    assert all(stop["reason"] == REASON_CLIPPED for stop in result.not_measured)


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


@pytest.mark.parametrize("accepted", [True, False])
def test_done_requires_every_stimulus_at_the_last_stop(monkeypatch, accepted):
    signals = plan_run.RunSignals()
    verdicts = iter([TakeVerdict(True), TakeVerdict(accepted, None if accepted else REASON_CLIPPED,
                     next="accept" if accepted else "retake_same", charge="speaker")])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    request = _walk([0])
    request = replace(request, template=replace(request.template, level_ladder_dbfs=(-24, -18)))
    def analyze(record):
        signals.complete.set()
        return _analysis(record)
    result, fakes = asyncio.run(_run_gated(request, analyze=analyze, signals=signals))
    assert len(fakes.banked) == 2
    assert result.status == ("complete" if accepted else "partial")
    assert result.takes_skipped == (0 if accepted else 1)


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
    assert not (root / "crossover_v2/run/round_receipt.json").exists()


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
    verdicts = iter([TakeVerdict(False, REASON_CLIPPED, next="retake_same", charge="operator"),
                     TakeVerdict(True), TakeVerdict(False, REASON_CLIPPED, next="retake_same", charge="operator")])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    result, fakes = asyncio.run(_run_gated(replace(_walk([0], ("fp-a", "fp-b")), retries_per_pose=1)))
    assert len(fakes.banked) == 3
    assert result.status == "partial"


@pytest.mark.parametrize("case,status,faults", [
    ("accepted", "complete", [None]),
    ("refused", "complete", [REASON_DRIFT_BASELINES_DISAGREE, None]),
    ("drift", "complete", [None, REASON_LEVEL_DRIFT_AT_SESSION_GAIN, None]),
    ("analysis_error", "partial", [REASON_INTERNAL_ERROR]),
])
def test_a_take_banks_the_verdict_and_level_the_run_judged(case, status, faults):
    """The bank judges each take before it writes the record, so every banked
    take holds the verdict and level the run decided it by, a refused one and
    one whose analysis failed too (ADR-0383, ADR-0395)."""
    if case == "drift":
        result, fakes, _, _ = _run_levelled(replace(_walk([0]), repeats=2), (70.0, 73.0, 70.0))
    else:
        calls = count(1)
        def analyze(record):
            if case == "analysis_error":
                raise ValueError("bad capture")
            return replace(_analysis(record), discontinuity_samples=1024 if case == "refused" and next(calls) == 1 else 0)
        result, fakes = asyncio.run(_run_gated(replace(_walk([0]), retries_per_pose=0), analyze=analyze))
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
    """The take whose assessor raised banks a stop and is logged; its capture's
    later rung banks unassessed, and the error ends the run once the capture has
    played (ADR-0383)."""
    fakes, assessor = FakeSeams(), Mock(side_effect=RuntimeError)
    request = _walk([0, 20])
    request = replace(request, template=replace(request.template, level_ladder_dbfs=(-24, -18)))
    with caplog.at_level("WARNING", logger=plan_run.logger.name), pytest.raises(RuntimeError):
        asyncio.run(_run_gated(request, seams=fakes, assessor=assessor))
    assert (assessor.call_count, fakes.play.rungs) == (1, [-24, -18])
    assert [(record["verdict"]["fault"], record["verdict"]["evidence"]) for record in fakes.banked] == [
        (REASON_INTERNAL_ERROR, {"error_type": "RuntimeError"}), (REASON_INTERNAL_ERROR, {"assessed": False})]
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
    """Neither a pose nor the window it picks (ADR-0400) is a set boundary."""
    manifest = RunManifest("run", _Store(FakeSeams().records))
    record = {"candidate_id": "", "graph_fingerprint": "graph", "stimulus_id": "program",
              "level_db": -20.0, "stimulus_dbfs": -18.0, "pose_kind": "bearing", "gating_applied": True,
              "side": "left", "role": "summed"}
    async def append():
        for index, degrees in enumerate([0, 10, 20], 1):
            manifest.begin({"index": index, "repeat": 1, "pose": {"azimuth_deg": degrees}}, attempt=1, pose_index=index - 1)
            await manifest.append({**record, "take_id": manifest.allocate_take_id(), **(changed if index == 2 else {})}, f"record-{index}",
                                  TakeVerdict(True), complete=True, level_observation={})
    asyncio.run(append())
    groups = manifest.to_dict()["sets"]
    split = bool(set(changed) - {"pose_kind", "gating_applied"})
    assert len(groups) == (2 if split else 1)
    poses = {take["take_id"]: take["pose"]["azimuth_deg"] for take in manifest.takes}
    assert {poses[t["take_id"]] for t in groups[0]["takes"]} == ({0, 20} if split else {0, 10, 20})


def test_manifest_names_emitted_role_levels():
    manifest = RunManifest("run", _Store(FakeSeams().records))
    manifest.begin({"index": 1, "repeat": 1, "pose": {"azimuth_deg": 0}}, attempt=1, pose_index=0)
    program = build_measure_program({"woofer": -18.0, "tweeter": -24.0}, [
        RoleBand("woofer", 0, FrequencyBand(20, 2000)), RoleBand("tweeter", 1, FrequencyBand(1500, 20000))])
    record = {"take_id": manifest.allocate_take_id(), "stimulus_dbfs": -12, "program": program.to_dict(),
              "curves": [{"role": "woofer", "band_hz": [20, 2000], "validity_floor_hz": 100}]}
    asyncio.run(manifest.append(record, "record", TakeVerdict(True), complete=True, level_observation={}))
    groups = manifest.to_dict()["sets"]
    assert {group["capture_basis"]["role"]: group["capture_basis"]["stimulus_dbfs"]
            for group in groups} == {"woofer": -18, "tweeter": -24}
    assert manifest.takes_measured == 1


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
    request = replace(_walk([0, 20, -20], ("base",)), repeats=2)
    captures = plan_run.prepare_plan_captures(request) if prepared else None
    assert calls == []
    result, fakes = asyncio.run(_run_gated(request, captures=captures))
    assert len(calls) == 1
    if available:
        assert result.status == "complete"
        assert [spec.candidate_id for spec in result.specs.values()] == ["banked-base"] * (6 + request.repeats * prepared)
        assert fakes.graph.scopes == [("timing", "banked-base")] * (request.repeats * prepared) + [("candidate", "banked-base")] * 6
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
    """A speaker baseline takes its timing at entry, then each driver at each
    pose, and no room sweep: room evidence comes from a room or rear seat
    round (ADR-0400)."""
    program = run_preset("speaker", layout)
    request = ac.request_for_preset(program, repeats=2)
    captures = plan_run.prepare_plan_captures(request)
    timing = [capture for capture in captures if capture.spec.graph_scope == "timing"]
    assert [(capture.stop.pose.azimuth_deg, capture.repeat) for capture in timing] == [(0, 1), (0, 2)]
    assert {capture.stop.purpose for capture in captures} == {"speaker"}
    assert [capture.stop.pose.place for capture in captures if capture.spec.program_phase == "measure"] == [
        pose.place for pose in program.poses for _ in range(pose.repeats * 2)]


def test_a_hand_written_branch_plan_resolves_its_timing_take_as_a_summed_take():
    plan = ac.AngleCaptureRequest(
        (ac.AngleStop(Pose(0, 0), ac.REGIME_BRANCHES, branch_pair="front_rear", purpose="speaker"),), program="speaker/mark",
    )
    request = ac.AngleCaptureRequest.from_mapping(json.loads(json.dumps(plan.to_dict())))
    captures = plan_run.prepare_plan_captures(request, roles_bands=tuple(_roles()))

    entry = next(c for c in captures if c.spec.program_phase == "timing")
    assert (entry.stop.regime, entry.stop.branch_pair) == (ac.REGIME_SUMMED, "drivers")
    assert entry.spec.branch_target_ids == ()
    assert {c.spec.branch_target_ids for c in captures if c.spec.graph_scope == "candidate_branches"} == {
        ("woofer", "woofer:rear")}


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
    ("summed", "base", "bass", "bass/axis", ("lateral",), None),
    ("summed", "base", "reference", "nearfield/each", ("lateral",), None),
])
@pytest.mark.parametrize("repeats", [1, 3])
def test_inline_plan_derives_only_the_preparation_it_needs(regime, candidate, purpose, program, phases, scope, repeats):
    """The timing take is the named preset's (its ``timing_take`` flag), taken on the base; a plan
    naming no preset takes none."""
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(Pose(20, 0), regime, candidate_id=candidate, purpose=purpose),),
        candidates=(candidate,), repeats=repeats, program=program,
    )
    captures = plan_run.prepare_plan_captures(request)
    phase_repeats = {"check": 1, "timing": repeats, "measure": repeats, "lateral": repeats}
    assert tuple(capture.spec.program_phase for capture in captures) == tuple(
        phase for phase in phases for _ in range(phase_repeats[phase]))
    assert [capture.repeat for capture in captures[-repeats:]] == list(range(1, repeats + 1))
    assert [capture.stop.pose.azimuth_deg for capture in captures[-repeats:]] == [20] * repeats
    assert all(capture.stop.pose.azimuth_deg == 0 for capture in captures[:-repeats])
    baseline = [capture for capture in captures if capture.spec.program_phase == "timing"]
    assert [capture.repeat for capture in baseline] == (list(range(1, repeats + 1)) if scope else [])
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
    request = ac.AngleCaptureRequest.from_mapping(json.loads(json.dumps(ac.request_for_preset(program).to_dict())))
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
            played, _CHAIN_EXCITATION, None, safety_profile={}, role_targets={}).stimulus_segments()) if (
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
            program = program_for_spec(played, self.excitation, None, asked, safety_profile={}, role_targets={})
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


def _run_levelled(request, readings, *, replace_at=None, ceiling_db=0.0, redo_at=(), web=True, chain=None,
                  room_gain_db=0.0, verdicts=None, redo_when_unmeasured=None, caps_db=None):
    """A plan whose recordings pass, admitted by the conductor as a web run's are
    (or, not ``web``, run as the bass ladder runs it: no gate, no ``admit``) and
    judged on the level each take read; the microphone is re-placed at take
    ``replace_at``, and the operator presses Redo during each take in
    ``redo_at``, take 0 being just after the first placement is confirmed. A
    ``chain`` reads each take from its targets' sensitivities and the room's
    gain under 150 Hz (:class:`_BranchChain`), its drivers capped at ``caps_db``
    over 0 dBFS. ``verdicts`` answers a take by
    its number instead of its assessment, where it returns one. The operator
    presses Redo just as the planned take ``redo_when_unmeasured`` is left
    unmeasured."""
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
                request, session=session, manifest=manifest, gate=gate if web else None, aborts=_ABORTS,
                signals=signals, analyze=_heard_analysis, captures=captures, assessor=assessor,
                admit=(lambda i, a, e, ledger: conductor.authorize_begin(i, a, e, executor_ledger=ledger)) if web else None)

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
    assert fakes.play.rungs == [None, -29.0, -29.0, None, -29.0, None, -27.0, None, -29.0]
    assert selected == [False, True, False, False, True, False, True, False, True]
    steps = {(p["measurement"], p["attempt"]): p["level_step"] for p in gate.progress if "level_step" in p}
    assert [step == "probe" for step in steps.values()] == [rung is None for rung in fakes.play.rungs]
    assert [take["level"]["loudest_half_second_db_spl"] for take in sorted(
        _takes(result.joined()), key=lambda take: take["take_id"])] == [reading - 3 for reading in readings]


def test_a_near_field_take_its_ceiling_holds_quiet_is_kept_not_retaken():
    """A take its ceiling played under the peak it asked for is kept too quiet:
    a louder retake would replay it until the pose's retries ran out (ADR-0361)."""
    request = ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, kind="close", distance_m=0.03, driver="woofer"),), purposes=("reference",), stimulus=NEAR_FIELD))

    result, fakes, selected, _ = _run_levelled(request, (66.0, 77.0), ceiling_db=-30.0)

    assert result.status == "complete"
    assert (fakes.play.rungs, selected) == ([None, -29.0], [False, True])


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
        runs.append(([capture.spec for capture in captures], fakes.play.rungs, selected,
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

    assert fakes.play.rungs == [None, -33.0] * 2 + [None, -27.0] * 2
    *live, ended = gate.progress
    assert [(finding["role"], finding["pose"]["distance_m"], finding["spread_db"])
            for finding in ended["level_mismatches"]] == [("woofer", 0.015, 6.0), ("woofer", 0.03, 6.0)]
    assert [len(facts["level_mismatches"]) for facts in live] == [int(facts["pose"] == 4) for facts in live]
    lines = set(round_lines(ended)) - set(round_lines({**ended, "level_mismatches": []}))
    assert len(lines) == 2 and lines <= set(coverage_lines({}, result.joined()))
    assert len(lines & set(round_lines(live[-1]))) == 1
    assert sum(getattr(record, "jasper_event", "") == "active_speaker.driver_level_mismatch"
               for record in caplog.records) == 2


def test_a_far_field_take_keeps_the_drift_rule_and_is_never_levelled():
    """Only a take at one driver's pose is held to the near-field target: a
    far-field repeat that reads 3 dB off its first is retaken as drift, at the
    same level (ADR-0361)."""
    result, fakes, selected, gate = _run_levelled(replace(_walk([0]), repeats=2), (70.0, 73.0, 70.0))

    assert result.status == "complete"
    assert fakes.play.rungs == [None, None, None]
    assert selected == [True, False, True]
    assert not any("level_step" in progress for progress in gate.progress)


def test_a_close_driverless_spot_turns_itself_down_once():
    """rear_behind's spot 0.1 m behind the cabinet reads louder than the mark at
    the run's fader. It plays one probe of its own summed sweep, and its take is
    turned down to 80 dB; the mark before it plays at the fader and is never
    levelled (ADR-0403)."""
    result, fakes, selected, gate = _run_levelled(
        ac.request_for_preset(run_preset("rear/express", "rear_behind")), (75.0, 92.0, 80.0))

    assert result.status == "complete"
    assert fakes.play.rungs == [None, None, -55.0]
    assert selected == [True, False, True]
    steps = {(p["measurement"], p["attempt"]): p["level_step"] for p in gate.progress if "level_step" in p}
    assert list(steps.values()) == ["probe", "levelled"]


def test_a_close_driverless_set_shares_one_level():
    """A close driverless set probes once, at its first take. Its repeats and its
    lateral pose play at the level that take landed and answer to their repeats,
    so a lateral that reads 4 dB under the target is not raised (ADR-0403)."""
    request = replace(ac.request_for_preset(run_preset("rear/express", poses=json.dumps([
        {"azimuth_deg": angle, "elevation_deg": 0, "distance_m": 0.5} for angle in (0, 20)]))), repeats=2)

    result, fakes, selected, _ = _run_levelled(request, (90.0, 80.0, 80.4, 76.0, 76.3))

    assert result.status == "complete"
    assert fakes.play.rungs == [None] + [-53.0] * 4
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
        program = program_for_spec(spec, _CHAIN_EXCITATION, None, call["stimulus_dbfs"], safety_profile={},
                                   role_targets={})
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
    one = replace(ac.request_for_preset(run_preset("rear/pair", None, "0")), repeats=2)
    two = ac.request_for_preset(run_preset("rear/pair", None, "0,80"))
    composer = partial(program_for_spec, excitation=_CHAIN_EXCITATION, gain_plan_db=None, safety_profile={},
                       role_targets={})
    captures = {name: plan_run.prepare_plan_captures(request) for name, request in (("one", one), ("two", two))}
    facts = {name: plan_run.schedule_facts([(plan_run._pose(capture.stop), capture.spec) for capture in rows],
                                           composer, mover="arm") for name, rows in captures.items()}
    probe_s = sum(probe.total_samples / probe.sample_rate_hz for probe in map(composer, branch_probes(
        captures["two"][-1].spec)))

    assert {name: [capture.spec.level_probe for capture in rows] for name, rows in captures.items()} == {
        "one": [True, False], "two": [True, True]}
    assert facts["two"]["estimated_seconds"] - facts["one"]["estimated_seconds"] == pytest.approx(probe_s)


_BEHIND = [{"azimuth_deg": 0, "elevation_deg": 0},
           *({"azimuth_deg": angle, "elevation_deg": 0, "kind": "behind", "distance_m": 0.1} for angle in (0, 20))]


def test_each_graph_of_a_close_set_probes_once():
    """A close set is one candidate graph: an A/B pair at two behind spots, with
    repeats, plays one probe per graph, at that graph's first take, and the
    graph's repeats and lateral take carry its level (ADR-0406)."""
    request = replace(ac.request_for_preset(run_preset("rear/express", poses=_BEHIND), candidates=("base", "trial")),
                      repeats=2)

    captures = plan_run.prepare_plan_captures(request)

    behind = [(ac.candidate_identity(capture.stop.candidate_id), capture.spec.level_probe, start)
              for capture, start in zip(captures, ac.level_sets([capture.stop for capture in captures],
                                                                [capture.spec.graph_scope for capture in captures]))
              if capture.stop.pose.kind == "behind"]
    firsts = {candidate: index for index, (candidate, _, _) in reversed(list(enumerate(behind)))}
    assert [probe for _, probe, _ in behind] == [index in firsts.values() for index in range(len(behind))]
    assert sorted(firsts) == ["base", "trial"]
    assert len({start for _, _, start in behind}) == 2


@pytest.mark.parametrize(("purposes", "probes"), [(("bass", "speaker"), [True, True]),
                                                  (("rear", "speaker"), [True, False])])
def test_a_close_set_is_the_graph_its_takes_play(purposes, probes):
    """Two purposes at one close spot share a probe only when their takes play
    one graph: a bass take on the base plays its room and bass layers cleared,
    a rear or speaker take plays none cleared (ADR-0406)."""
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
    request = replace(ac.request_for_preset(run_preset("rear/express", poses=_BEHIND), candidates=("base", "trial")),
                      repeats=2)
    report = preflight_levels(request, ready_facts(request, applied_rear_plays=False,
                                                   candidates={"trial": _cardioid_trial()}))
    assert not report.blocking

    result, _, _ = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": 100.0, "behind": 110.0},
        graph_db={("candidate", "trial", "bearing"): _REAR_SUM_DB, ("candidate", "trial", "behind"): 15.0},
        margin_db=report.rung_admission["run_margin_db"]))

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
    request = ac.AngleCaptureRequest(stops=(
        ac.AngleStop(pose, ac.REGIME_SUMMED, purpose="rear"),
        ac.AngleStop(pose, ac.REGIME_BRANCHES, purpose="rear", branch_pair="front_rear")), repeats=2)

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

    result, fakes, _, _ = _run_levelled(
        request, (), ceiling_db=-12.0, chain=_REAR_UP,
        verdicts=lambda take: clipped if take == 3 else _OVERRUN if 4 <= take <= 7 else None)

    assert fakes.play.rungs == [None, None, -39.0] + [-42.0] * 4 + [-42.0]
    assert [call["spec"].branch_levels_dbfs for call in fakes.play.calls] == [
        (), (), (-27.0, -33.0), *[(-30.0, -36.0)] * 5]
    assert [row["reason"] for row in result.not_measured] == [REASON_CAPTURE_OVERRUN]


def test_a_close_set_that_finds_no_level_plays_no_more_takes():
    """The behind spot's probe never reads over the room, so its set found no
    level. The rest of the set does not play (ADR-0361 §3, ADR-0403 §3)."""
    request = replace(ac.request_for_preset(run_preset("rear/express", "rear_behind")), repeats=2)

    result, fakes, _, _ = _run_levelled(request, (75.0, 75.0) + (70.0,) * 8,
                                        verdicts=lambda take: _UNHEARD if take >= 3 else None)

    assert _takes_played_unlevelled(fakes)[2:] == []
    assert [row["reason"] for row in result.not_measured] == [REASON_SNR_FLOOR, REASON_LEVEL_UNSOLVED]


@pytest.mark.parametrize("readings, verdicts, rungs, reasons", [
    ((66.0,) + (86.0,) * 6 + (80.0,), None,
     [None, -29.0, -36.0, -43.0, -50.0, -57.0, -64.0, -71.0], [REASON_LEVEL_OFF_TARGET]),
    ((60.0,) * 8, lambda take: _UNHEARD, [None] * 4, [REASON_SNR_FLOOR, REASON_LEVEL_UNSOLVED]),
], ids=["solved", "unsolved"])
def test_a_driver_pose_whose_first_take_never_lands_carries_its_level_or_skips(readings, verdicts, rungs, reasons):
    """Unlike main, where the next take probes again: a driver's next take at
    the same placement plays at the last level solved for it, with no probe,
    or, when its placement found none, does not play (ADR-0361 §3)."""
    request = ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, repeats=2, kind="close", distance_m=0.015, driver="woofer"),), purposes=("reference",),
        stimulus=NEAR_FIELD))

    result, fakes, _, _ = _run_levelled(request, readings, verdicts=verdicts)

    assert fakes.play.rungs == rungs
    assert [row["reason"] for row in result.not_measured] == reasons


@pytest.mark.parametrize("redo_at", [3, 4], ids=["during-the-rear-probe", "after-the-take"])
def test_a_redo_probes_both_branches_again_behind_a_summed_take(redo_at):
    """A summed take at the mark, then a branch set at the mark: a Redo there
    plays the summed take again and both probes again before the branch take,
    never the take at no level or at its old one (ADR-0365, ADR-0403 §3)."""
    request = ac.AngleCaptureRequest(stops=(
        ac.AngleStop(Pose(0, 0), ac.REGIME_SUMMED, purpose="rear"),
        ac.AngleStop(Pose(0, 0), ac.REGIME_BRANCHES, purpose="rear", branch_pair="front_rear")))

    result, fakes, _, _ = _run_levelled(request, (), ceiling_db=-12.0, redo_at={redo_at},
                                        chain={"woofer": 110.0, "woofer:rear": 110.0})

    assert result.status == "complete"
    plays = [(call["spec"].graph_scope, call["stimulus_dbfs"]) for call in fakes.play.calls]
    assert plays[redo_at:] == [("candidate", None), ("drivers", None), ("drivers", None),
                               ("candidate_branches", -37.0)]


def test_a_redo_as_a_set_finds_no_level_plays_the_rest_at_the_level_it_then_lands():
    """The behind spot's probe is never heard, and the operator presses Redo just
    as that take is left unmeasured. The redone take lands, and the rest of the
    set plays at its level, not skipped as level_unsolved (ADR-0403 §3)."""
    request = replace(ac.request_for_preset(run_preset("rear/express", "rear_behind")), repeats=2)

    result, fakes, _, _ = _run_levelled(request, (75.0, 75.0) + (70.0,) * 4 + (92.0, 80.0, 80.0),
                                        verdicts=lambda take: _UNHEARD if 3 <= take <= 6 else None,
                                        redo_when_unmeasured=3)

    assert result.status == "complete" and result.not_measured == []
    assert fakes.play.rungs == [None] * 7 + [-55.0, -55.0]


def test_a_close_set_whose_first_take_never_lands_plays_on_at_its_last_solved_level():
    """A close set's first take that reads loud at every level is left unmeasured
    once its retakes are spent. The rest of its set plays at the last level solved
    for it, never at the take's ceiling (ADR-0403)."""
    request = replace(ac.request_for_preset(run_preset("rear/express", "rear_behind")), repeats=2)

    result, fakes, _, _ = _run_levelled(request, (75.0, 75.0, 92.0) + (86.0,) * 6 + (80.0,))

    assert fakes.play.rungs == [None, None, None, -55.0, -62.0, -69.0, -76.0, -83.0, -90.0, -97.0]
    assert [row["reason"] for row in result.not_measured] == [REASON_LEVEL_OFF_TARGET]


#: A two-way speaker's drivers, and CHECK's plan for them, as the fake run chain composes its plays.
_RUN_BANDS = {"woofer": FrequencyBand(20, 4000), "tweeter": FrequencyBand(1500, 20000)}
_RUN_GAINS = {"woofer": -20.0, "tweeter": -26.0}


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
                                   _RUN_GAINS, record.get("stimulus_dbfs"), safety_profile={}, role_targets={})
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


def _chain_door(fakes, manifest, caps, chain_db, scope_gains=None, graph_db=None, margin_db=0.0, finds=True):
    chain = _RunChain(manifest, fakes.play, caps, chain_db, scope_gains, graph_db)
    return plan_run.RunDoor(nullcontext(SimpleNamespace()), lambda opened, allocate: TuningSession(
        "run", replace(fakes, records=chain).seams(), opened.measurement_volume_db, allocate),
        _MIC, SimpleNamespace(model_key="minidsp_umik2"), 85.0, caps_dbfs=caps if finds else None, margin_db=margin_db)


def _accept_unlevelled(analysis, **kw):
    return TakeVerdict(True, next="accept") if kw["pose_level"] is None else capture_dispatch.assess(analysis, **kw)


async def _run_found(monkeypatch, request, *, caps, chain_db, gate=None, signals=None, events=None, assessor=None,
                     scope_gains=None, margin_db=0.0, graph_db=None):
    """A run through a door that finds its fader, on a fake chain; each take that
    does not level itself is accepted. Answers the result, the plays and the
    fader of each level window, in order. ``events`` logs each fader a window
    sets and the household level (None) it leaves when it closes."""
    windows: list = []
    _found_door(monkeypatch, windows, [] if events is None else events)
    fakes = FakeSeams(volume=_Fader())
    manifest = RunManifest("run", _Store(fakes.records))
    door = _chain_door(fakes, manifest, caps, chain_db, scope_gains, graph_db, margin_db)
    result = await plan_run.run_plan(
        request, door=door, manifest=manifest, analyze=_run_chain_analysis, gate=gate or AnsweredGate(),
        aborts=_ABORTS, signals=signals, captures=plan_run.prepare_plan_captures(request),
        assessor=assessor or _accept_unlevelled)
    return result, fakes.play.calls, windows


async def _ladder_found(monkeypatch, request, *, caps, chain_db, gate=None):
    """A ladder on a fake chain whose first rung finds its level through a door
    that states caps; the other rungs play at the level stated to them. Answers
    the ladder's signals, its rungs' results and plays, and each level window."""
    windows: list = []
    _found_door(monkeypatch, windows, [])
    fakes, signals = FakeSeams(volume=_Fader()), plan_run.RunSignals()
    ladder = preflight_levels(request, ready_facts(request))
    assert not ladder.blocking

    def prepare(plan):
        manifest = RunManifest(f"run-{len(fakes.play.calls)}", _Store(fakes.records))
        door = _chain_door(fakes, manifest, caps, chain_db, finds=plan.level.level_db is None)
        return LevelRun(manifest, door, _run_chain_analysis, _accept_unlevelled, prepare_level_captures(plan))

    results = await run_levels(ladder, hold=nullcontext(SimpleNamespace()), prepare=prepare,
                               gate=AnsweredGate() if gate is None else gate, aborts=_ABORTS, signals=signals)
    return signals, results, fakes.play.calls, windows


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


def test_a_run_whose_first_spot_is_a_seat_lands_it_under_74_db(monkeypatch):
    """A first seat spot is levelled 1 dB under 74 dB, and the other seat spots
    hold that fader (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("room", "seat_express"), level=ac.LevelPolicy(level_db=0.0))

    result, plays, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"seat": 94.0}))

    assert windows == [0.0, -9.0] and len(plays) == 4
    first = min((take for take in _takes(result.joined()) if take["selected"]), key=lambda take: take["index"])
    assert first["capture_integrity"]["spl"]["max_window_db_spl"] == pytest.approx(73.0)


@pytest.mark.parametrize(("tweeter_cap", "chain_db", "timing_gain", "margin_db", "fader", "timing_db", "pair_db"), [
    (-6.0, 100.0, 0.0, 0.0, -9.0, 79.0, 79.0), (-6.0, 100.0, 6.0, 0.0, -3.0, 79.0, 79.0),
    (-6.0, 100.0, 6.0, 0.0, -3.0, 79.0, None), (-6.0, 100.0, 0.0, 7.0, -12.0, 76.0, 76.0),
    (-20.0, 97.0, 0.0, 9.0, -11.0, 74.0, 74.0)],
    ids=["under the stop", "backoff, a pair that probes its graphs", "backoff on one graph", "lift and rise",
         "lift and rise over a take the cap holds"])
def test_the_run_fader_comes_down_by_what_its_margins_pass_the_stop_by(
        monkeypatch, tweeter_cap, chain_db, timing_gain, margin_db, fader, timing_db, pair_db):
    """A speaker run probes its timing take, which lands 1 dB under 80 dB, or at
    the tweeter's cap when that holds it lower. Its landed reading, the 2 dB
    tolerance and the margins stated for its other takes bring the takes down
    only by how far their sum passes the 85 dB stop. A trial's A/B pair probes its
    own graphs there (ADR-0408), so the timing graph's own backoff adds nothing.
    A take the cap holds at the output comes down too, as the fader drops far
    enough to free it (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("speaker", "speaker_mark"),
                                    candidates=("base", "trial") if pair_db is not None else ())

    result, _, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": tweeter_cap}, chain_db={"bearing": chain_db},
        scope_gains={"timing": dict.fromkeys(("woofer", "tweeter"), timing_gain)}, margin_db=margin_db))

    assert result.status == "complete" and windows == [0.0, pytest.approx(fader, abs=0.02)]
    read = {(take["phase"], take["candidate_id"]): take["capture_integrity"]["spl"]["max_window_db_spl"]
            for take in _takes(result.joined()) if take["selected"] and take["phase"] in ("timing", "lateral")}
    pair = {("lateral", "banked-base"), ("lateral", "trial")} if pair_db is not None else set()
    assert set(read) == {("timing", "banked-base")} | pair
    assert read == {key: pytest.approx(timing_db if key[0] == "timing" else pair_db, abs=0.02) for key in read}


@pytest.mark.parametrize("shape", ["no summed take", "check before the probe"])
def test_a_take_at_the_run_fader_before_its_probe_refuses_the_run_before_anything_plays(monkeypatch, shape):
    """A run that finds its fader plays a take that does not level itself only
    after the probe that found that fader, so a run where such a take would play
    first, or with no probe at all, is refused before any take plays (ADR-0403 §4)."""
    result, plays, windows = asyncio.run(_run_found(
        monkeypatch, _unprobed_plans()[shape], caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": 100.0}))

    assert (result.reason, plays, windows) == (ac.WALK_LEVEL_POLICY_INVALID, [], [])


def test_a_later_spot_that_does_not_level_itself_probes_before_its_first_take(monkeypatch):
    """A plan whose first spot levels itself (a close set behind the cabinet)
    plays it at the probe fader, where it finds its own level. Its later spot,
    at the mark, levels nothing, so its first summed take is probed under that
    spot's own placement before its first take, which then plays at the fader
    the probe found (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("rear/express", poses=[
        {"azimuth_deg": 0, "elevation_deg": 0, "kind": "behind", "distance_m": 0.1},
        {"azimuth_deg": 0, "elevation_deg": 0}]))
    gate = AnsweredGate()

    result, plays, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"behind": 110.0, "bearing": 100.0},
        gate=gate))

    probes = [index for index, call in enumerate(plays) if call["spec"].level_probe and call["stimulus_dbfs"] is None]
    assert probes[0] == 0 and len(probes) == 2 and len(gate.grants) == 2
    assert {call["level_db"] for call in plays[:probes[1]]} == {0.0}
    assert [(call["spec"].level_probe, call["level_db"]) for call in plays[probes[1]:]] == [(True, 0.0), (False, -9.0)]
    assert windows == [0.0, -9.0] and result.status == "complete"
    mark = max((take for take in _takes(result.joined()) if take["selected"]), key=lambda take: take["index"])
    assert mark["capture_integrity"]["spl"]["max_window_db_spl"] == pytest.approx(79.0)
    assert {key: result.level["run"][key] for key in ("level_db", "probe_fader_db", "source")} == {
        "level_db": -9.0, "probe_fader_db": 0.0, "source": "probe"}


_CLOSE_SET = {"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", "distance_m": 0.3}
_DRIVER_POSE = {"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", "distance_m": 0.3, "driver": "woofer"}
_MARK = {"azimuth_deg": 0, "elevation_deg": 0}


def _ladder_plans():
    """Ladders of two steps. The two whose first pose levels every take itself are
    the review's; a later pose of drivers alone has no summed take of its own."""
    def stepped(program, poses):
        return ac.request_for_preset(run_preset(program, poses=poses), targets=("woofer", "tweeter"), levels=(-20.0, -25.0))

    return {"mark first": stepped("rear/express", [_MARK, _CLOSE_SET]),
            "drivers alone at a later pose": ac.AngleCaptureRequest(
                (ac.AngleStop(Pose(0, 0), ac.REGIME_SUMMED, purpose="rear"),
                 ac.AngleStop(Pose(20, 0), ac.REGIME_PER_DRIVER, purpose="speaker")),
                program="rear/express", levels=(-20.0, -25.0)),
            "close set first": stepped("rear/express", [_CLOSE_SET, _MARK]),
            "driver pose first": stepped("speaker/mark", [_DRIVER_POSE, _MARK])}


@pytest.mark.parametrize(("shape", "rungs"), [
    ("mark first", 4), ("drivers alone at a later pose", 4), ("close set first", 1), ("driver pose first", 1)])
def test_a_ladder_plays_only_under_the_level_its_first_rung_found(monkeypatch, shape, rungs):
    """A ladder's first rung at its first pose finds the level its rungs step
    under, and every later rung plays its step under that level, at a pose with
    no summed take too. A first pose where every take levels itself finds none,
    so the ladder ends there: no later pose plays at a level no probe found
    (ADR-0403 §4)."""
    signals, results, _, windows = asyncio.run(_ladder_found(
        monkeypatch, _ladder_plans()[shape], caps={"woofer": 0.0, "tweeter": -6.0},
        chain_db={"bearing": 100.0, "close": 110.0}))

    summed = [take["capture_integrity"]["spl"]["max_window_db_spl"] for result in results
              for take in _takes(result.joined())
              if take["selected"] and take["pose_kind"] == "bearing" and take["phase"] in ("timing", "lateral")]
    found = rungs == 4
    assert (len(results), signals.stop.is_set() and signals.stop_reason) == (rungs, not found and REASON_LEVEL_UNSOLVED)
    assert windows == ([0.0, -9.0, -14.0, -9.0, -14.0] if found else [0.0])
    assert summed == pytest.approx([79.0, 74.0] if found else [], abs=0.02)


@pytest.mark.parametrize(("levels", "stated", "rungs"), [
    ("auto", None, len(LEVEL_OFFSETS_DB)), (None, (-20.0, -25.0), 2), (None, None, 1)],
    ids=["the preset's whole ladder", "the steps a plan states", "no ladder"])
def test_a_ladder_plays_each_placements_captures_at_every_rung(levels, stated, rungs):
    """A ladder finishes a placement's captures at each rung before the microphone moves (``run_levels``)."""
    request = ac.request_for_preset(run_preset("room", "seat_express"), candidates=("base", "trial"), levels=stated)
    captures = prepare_level_captures(request)

    played = ladder_captures(request, levels, captures)

    by_place = [list(group) for _, group in groupby(captures, key=lambda capture: capture.stop.pose.place)]
    assert len(by_place) == 3 and all(len(group) == 2 for group in by_place)
    assert list(played) == [capture for group in by_place for _ in range(rungs) for capture in group]


def test_a_ladders_frames_name_the_program_the_page_words_them_by(monkeypatch):
    """A ladder's own frames replace the run's facts, so each names the program whose headline the page shows."""
    request, gate = _ladder_plans()["mark first"], AnsweredGate()
    asyncio.run(_ladder_found(monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0},
                              chain_db={"bearing": 100.0, "close": 110.0}, gate=gate))

    frames = [frame for frame in gate.progress if frame.get("status") == "running"]
    assert frames and {frame["program"] for frame in frames} == {request.program}
    page = build_crossover_envelope_v2({
        "active": True, "setup": {"active": True, "status": "ready"}, "crossover_v2": {"phase": "lateral"},
        "capture": {"status": "awaiting_capture", "run": frames[-1]}})
    assert page["verdict_text"] == next(
        row.run_headline for row in PROGRAM_ROWS if row.purpose == run_purpose(request.program))


def test_a_first_seat_spot_reads_at_most_76_db_over_a_lift(monkeypatch):
    """A run probed at its first seat spot comes down by what its margins pass
    76 dB by, not the 85 dB stop, so an A/B trial whose dynamic bass may lift
    6 dB over the probe's graph reads at most 76 dB there (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("room", "seat_express"), candidates=("base", "trial"))

    result, _, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"seat": 94.0},
        graph_db={("candidate", "trial"): 6.0}, margin_db=6.0))

    seat1 = [take for take in _takes(result.joined()) if take["selected"] and take["seat_offset_m"] == request.stops[0].pose.seat_offset_m]
    read = {take["candidate_id"]: take["capture_integrity"]["spl"]["max_window_db_spl"] for take in seat1}
    assert windows == [0.0, pytest.approx(-14.0, abs=0.02)] and result.status == "complete"
    assert read == {"banked-base": pytest.approx(68.0, abs=0.02), "trial": pytest.approx(74.0, abs=0.02)}


def test_an_ab_trial_over_a_room_boost_lands_under_76_db_at_its_first_seat(monkeypatch, tuning_profile):
    """An A/B trial's takes on the candidates' own graphs play a 6 dB room boost.
    A run probed on a candidate's own graph plays that boost in its probe, so it
    adds no margin, and its first seat spot reads under 76 dB (ADR-0403 §4,
    ADR-0385)."""
    request = ac.request_for_preset(run_preset("room", "seat_express"), candidates=("base", "trial"))
    report = preflight_levels(request, ready_facts(request, candidates={"trial": _room_candidate(tuning_profile)}))
    assert not report.blocking and report.rung_admission["run_margin_db"] == 0.0

    result, _, _ = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"seat": 94.0},
        graph_db=dict.fromkeys((("candidate", "banked-base"), ("candidate", "trial")), 6.0),
        margin_db=report.rung_admission["run_margin_db"]))

    read = [take["capture_integrity"]["spl"]["max_window_db_spl"] for take in _takes(result.joined())
            if take["selected"] and take["phase"] == "lateral"]
    assert result.status == "complete" and len(read) == len(request.stops) and max(read) < 76.0


def test_a_cardioid_ab_trial_over_a_probe_that_mutes_the_rear_lands_under_76_db(monkeypatch):
    """A cardioid trial plays the front and rear woofers in phase where the seat
    probe's graph mutes the rear. Preflight's margin counts their coherent sum, so
    every take at the first seat spot reads at or under 76 dB (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("room", "seat_express"), candidates=("base", "trial"))
    report = preflight_levels(request, ready_facts(request, applied_rear_plays=False,
                                                   candidates={"trial": _cardioid_trial()}))
    assert not report.blocking

    result, _, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"seat": 94.0},
        graph_db={("candidate", "trial"): _REAR_SUM_DB}, margin_db=report.rung_admission["run_margin_db"]))

    first = request.stops[0].pose.place
    read = [take["capture_integrity"]["spl"]["max_window_db_spl"] for take in _takes(result.joined())
            if take["selected"] and (take["pose_kind"], take["seat_offset_m"]) == (first[0], first[4])]
    assert result.status == "complete" and windows[-1] < windows[0] and max(read) <= 76.0 + 1e-6


def test_a_room_trial_comes_down_by_its_probes_backoff_for_a_graph_at_its_fader(monkeypatch):
    """A room trial's pair plays at the run's fader, found by the base's probe at
    the first seat spot. That probe's graph backs off 6 dB, so a take on another
    graph may play up to 6 dB over it, and the run comes down by that too: the
    trial reads at most 76 dB there (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("room", "seat_express"), candidates=("base", "trial"))

    result, _, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"seat": 94.0},
        scope_gains={"candidate": dict.fromkeys(("woofer", "tweeter"), 6.0)}, graph_db={("candidate", "trial"): 6.0}))

    read = {take["candidate_id"]: take["capture_integrity"]["spl"]["max_window_db_spl"]
            for take in _takes(result.joined()) if take["selected"]}
    assert result.status == "complete" and windows == [0.0, pytest.approx(-8.0, abs=0.02)]
    assert read == {"banked-base": pytest.approx(68.0, abs=0.02), "trial": pytest.approx(74.0, abs=0.02)}


#: jts3's applied bass extension at the smoke test (#6113): a 14.78 dB reserve (ADR-0359).
_JTS3_BASS = {"linkwitz_transform": {"source_hz": 112.8, "source_q": 1.23, "target_hz": 40.0, "target_q": 0.707},
              "delta_highpass_hz": 30.0, "detector_lowpass_hz": 120.0, "compressor_threshold_dbfs": -15.0}


@pytest.mark.parametrize(("box", "over_db"), [
    ("a two-way's room boost", {"banked-base": 6.0, "trial": 6.0}),
    ("a cardioid tune", {"banked-base": 3.32 + _REAR_SUM_DB, "trial": 3.32 + _REAR_SUM_DB}),
    ("jts3's bass and rear seed", {"banked-base": 14.78 + _REAR_SUM_DB, "trial": _REAR_SUM_DB})])
def test_over_a_timing_take_each_candidate_graph_probes_itself(monkeypatch, box, over_db):
    """A speaker trial's timing take finds the run's fader and plays at its own
    probe's level, with no margin cut, however much louder the candidates' graphs
    play: jts3's applied tune adds its 14.78 dB bass reserve and a rear seed, and
    its trial keeps the tweeter's trim. Each candidate graph's first take at the
    mark probes that graph, so every take of the pair lands at 80 ± 2 dB (ADR-0408)."""
    request = ac.request_for_preset(run_preset("speaker", "speaker_mark"), candidates=("base", "trial"))
    trial = replace(_cardioid_trial(), role_attenuations_db={"woofer": 0.0, "tweeter": -25.2})
    report = preflight_levels(request, ready_facts(request, applied_bass_extension=_JTS3_BASS, applied_rear_plays=True,
                                                   candidates={"trial": trial}))
    assert not report.blocking and report.rung_admission["run_margin_db"] == 0.0

    result, _, _ = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -25.0}, chain_db={"bearing": 110.0},
        graph_db={("candidate", name): db for name, db in over_db.items()},
        margin_db=report.rung_admission["run_margin_db"]))

    takes = _takes(result.joined())
    timing = [take["capture_integrity"]["spl"]["max_window_db_spl"] for take in takes
              if take["selected"] and take["phase"] == "timing"]
    pair = [take["capture_integrity"]["spl"]["max_window_db_spl"] for take in takes
            if take["selected"] and take["phase"] == "lateral"]
    probed = sorted(take["candidate_id"] for take in takes
                    if take["phase"] == "lateral" and is_level_probe(ExcitationProgram.from_dict(take["program"])))
    assert result.status == "complete" and probed == ["banked-base", "trial"]
    assert timing == [pytest.approx(79.0)] and len(pair) == len(request.stops)
    assert all(78.0 <= db <= 82.0 for db in pair), pair


@pytest.mark.parametrize(("chain_db", "kept", "reason"), [(50.0, False, REASON_SNR_FLOOR),
                                                          (100.0, True, REASON_LEVEL_UNSOLVED)], ids=["buried", "kept"])
def test_a_run_probe_that_finds_no_level_ends_the_run_before_any_take(monkeypatch, chain_db, kept, reason):
    """A probe the room buries asks for the microphone again. The fader is back at
    the household level before each placement, so it sits at the probe fader only
    while the probe plays. A probe that never finds a level ends the run, and so
    does one an assessor keeps, since a probe is never kept: nothing plays at a
    fader no probe found (ADR-0365, ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("speaker", "speaker_mark"), level=ac.LevelPolicy(level_db=0.0))
    events: list = []

    result, plays, _ = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": chain_db},
        gate=_PlacementLog(events), events=events,
        assessor=(lambda analysis, **kw: TakeVerdict(True, next="accept")) if kept else None))

    assert result.reason == reason and (len(plays) == 1) is kept
    assert events == ["placement", 0.0, None] * len(plays)
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


def test_a_close_set_after_the_run_probe_plays_only_at_its_own_level(monkeypatch):
    """At the fader the run found, a close set still plays its own probe from
    −60 dBFS at the output and then its takes at the level it solves, never at
    the run's fader; no view reads a probe's set (ADR-0403 §4)."""
    request = ac.request_for_preset(run_preset("rear/express", "rear_behind"), level=ac.LevelPolicy(level_db=0.0))

    result, plays, windows = asyncio.run(_run_found(
        monkeypatch, request, caps={"woofer": 0.0, "tweeter": -6.0}, chain_db={"bearing": 100.0, "behind": 110.0}))

    assert windows == [0.0, -9.0]
    assert [(call["spec"].level_probe, call["stimulus_dbfs"]) for call in plays] == [
        (True, None), (False, None), (True, None), (True, -22.0)]
    close_probe = next(take for take in _takes(result.joined()) if take["pose_kind"] == "behind" and take["attempt"] == 1)
    assert ExcitationProgram.from_dict(close_probe["program"]).stimulus_segments()[0].effective_peak_dbfs == pytest.approx(-60.0)
    document = result.to_dict()
    assert sum(bool(row["capture_basis"].get("level_probe")) for row in document["sets"]) == 2
    assert not any(row["capture_basis"].get("level_probe") for row in view_sets(document))


@pytest.mark.parametrize("web", [True, False], ids=["web", "ladder"])
@pytest.mark.parametrize("retries", [0, 2])
def test_a_repeat_that_keeps_drifting_is_retaken_only_for_its_retries(web, retries):
    """A level-drift retake spends one of the pose's retries in a web run and in
    the bass ladder alike, so a repeat that keeps drifting is left unmeasured
    once they are spent, never retaken without end (#5722)."""
    result, _, selected, _ = _run_levelled(replace(_walk([0]), repeats=2, retries_per_pose=retries),
                                           (70.0,) + (73.0,) * (retries + 1), web=web)

    assert [row["reason"] for row in result.not_measured] == [REASON_LEVEL_DRIFT_AT_SESSION_GAIN]
    assert selected == [True] + [False] * (retries + 1)


@pytest.mark.parametrize("retries", [0, MAX_EXTRA_ATTEMPTS_PER_POSITION])
def test_a_redo_at_a_driver_pose_places_it_again_and_never_ends_the_round(retries):
    """Each redo asks for the microphone again and starts the pose over at its
    probe, with its retries, so redos past the pose's budget never end the
    round, even one with no retries; the page is told which plays are the
    probe, and a pose's takes play at the level its probe solved (ADR-0365)."""
    request = ac.request_for_preset(Preset("nearfield/each", tuple(
        Pose(0, 0, repeats=repeats, kind="close", distance_m=mm / 1000, driver="woofer")
        for mm, repeats in ((15, 1), (30, 2))), purposes=("reference",), stimulus=NEAR_FIELD), retries_per_pose=retries)
    redos = MAX_EXTRA_ATTEMPTS_PER_POSITION + 1
    # The operator presses Redo during each of the first probes, then lets each pose land.
    result, fakes, selected, gate = _run_levelled(request, (66.0,) * (redos + 1) + (80.0, 66.0, 80.0, 80.0),
                                                  redo_at=range(1, redos + 1))

    assert (result.status, result.reason, result.not_measured) == ("complete", "", [])
    assert [index for index, _ in gate.grants] == [1] * (redos + 1) + [2]
    assert fakes.play.rungs == [None] * (redos + 1) + [-29.0, None, -29.0, -29.0]
    assert selected == [False] * (redos + 1) + [True, False, True, True]
    steps = {(p["measurement"], p["attempt"]): p["level_step"] for p in gate.progress if "level_step" in p}
    assert list(steps.values()) == ["probe"] * (redos + 1) + ["levelled", "probe", "levelled", "levelled"]


@pytest.mark.parametrize("driver,repeats,retries,redo_first,left,retakes,reason", [
    (True, 2, 0, True, 0, 1, ""), (True, 2, 0, False, 0, 2, ""), (True, 6, 3, False, 3, 2, ""),
    (False, 2, 0, True, 0, 0, ""), (False, 2, 1, False, 0, 1, ""), (False, 6, 3, False, 2, 1, ""),
    (False, 2, 0, False, 0, 0, REASON_RETRIES_SPENT),
])
def test_a_redo_spends_no_retry_on_the_takes_it_plays_again(
        monkeypatch, driver, repeats, retries, redo_first, left, retakes, reason):
    """A redo during a pose's last take plays the pose again from its start, and
    each take it plays again is free (#5722): a far-field redo costs its own retry,
    and a driver's pose starts over with its retries (ADR-0361). The earlier takes
    stay banked, but neither kept nor the level the new placement is held to, so
    a placement that moved the level is not refused as drift. A redo before any
    take played only asks for the placement again; one the pose cannot pay for
    ends the round with its retries spent."""
    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    request = (ac.request_for_preset(Preset("nearfield/each", (
        Pose(0, 0, repeats=repeats, kind="close", distance_m=0.015, driver="woofer"),),
        purposes=("reference",), stimulus=NEAR_FIELD), retries_per_pose=retries) if driver else
        replace(_walk([0]), repeats=repeats, retries_per_pose=retries))
    placements = [(66.0, *(80.0,) * repeats)] * 2 if driver else [(70.0,) * repeats, (75.0,) * repeats]

    result, _, selected, gate = _run_levelled(request, placements[0] + placements[1],
                                              redo_at=(0,) if redo_first else (repeats + driver,))

    assert (result.status, result.reason) == ("partial" if reason else "complete", reason)
    assert [index for index, _ in gate.grants] == [1] * (1 if reason else 2)
    kept = 0 if reason else repeats
    assert selected == [False] * (len(selected) - kept) + [True] * kept
    final = gate.progress[-1]
    assert (final["budget"]["allowed"], final["budget"]["left"], final["retakes"]) == (retries, left, retakes)


@pytest.mark.parametrize("purpose,layout,entry,poses", [
    ("speaker", "baseline_express", True, 5), ("room", "seat_express", False, 3),
    ("rear", "rear_express", False, 3),
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


def _staged(name, layout, restaged):
    plan = ac.request_for_preset(run_preset(name, layout)).to_dict()
    for stop in plan["stops"]:
        stop.update(restaged)
    return ac.AngleCaptureRequest.from_mapping(json.loads(json.dumps(plan)))


@pytest.mark.parametrize("name,layout,restaged,banked", [
    ("speaker/mark", "speaker_mark", {}, {("speaker", ("speaker",))}),
    ("rear/seat", "seat_express", {}, {("rear", ("rear", "room"))}),
    ("rear/seat", "seat_express", {"purpose": "room", "purposes": ["room"]}, {("room", ("room",))}),
])
async def test_a_take_banks_the_purposes_its_stop_names(name, layout, restaged, banked):
    """A preset names its purposes on each stop, and a stop staged under a preset's id serves
    what it names, not the preset's (ADR-0336, ADR-0383)."""
    request = _staged(name, layout, restaged)
    result, fakes = await _run_gated(request, captures=plan_run.prepare_plan_captures(request),
                                     assessor=lambda *_args, **_kwargs: TakeVerdict(True, next="accept"))
    assert result.status == "complete"
    assert {(take["measurement_purpose"], tuple(take["purposes"])) for take in fakes.banked} == banked


@pytest.mark.parametrize("restaged", [{"purpose": "room"}, {"purposes": ["rear", "rear"]}])
def test_a_staged_stop_names_its_purpose_first_and_each_purpose_once(restaged):
    with pytest.raises(ac.LateralWalkRefused) as refused:
        _staged("rear/seat", "seat_express", restaged)
    assert refused.value.reason == ac.WALK_STOP_NO_LONGER_VALID


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


async def test_bass_levels_refuse_when_no_level_is_admissible():
    request = ac.AngleCaptureRequest(stops=(ac.AngleStop(Pose(0, 0), ac.REGIME_SUMMED, purpose="bass"),))
    ladder = level_ladder(request, ready_facts(request, commissioning_stop_db_spl=None))
    hold, prepare = Mock(), Mock()
    with pytest.raises(ac.LateralWalkRefused) as refused:
        await run_levels(ladder, hold=hold, prepare=prepare, gate=AnsweredGate(), aborts=_ABORTS)
    assert refused.value.reason == "walk_commissioning_stop_unset"
    assert not hold.mock_calls and not prepare.mock_calls


@pytest.fixture
def rung_spl(monkeypatch):
    measurements = {}
    bank = RunManifest.bank

    async def measured_bank(self, record):
        level = record["level_db"]
        return await bank(self, {**record, "stimulus_id": "bass-sweep", "capture_integrity": {
            "spl": measurements.get(round(level, 2), {"loudest_half_second_db_spl": 93 + level,
                                                      "max_window_db_spl": 93 + level, "ceiling_db_spl": 85})}})

    monkeypatch.setattr(RunManifest, "bank", measured_bank)
    return measurements


#: The fader a ladder's first rung finds: its probe climbs to the take's own −12 dBFS
#: ceiling, solves −18 dBFS, and the take lands 1 dB under 80 dB (ADR-0403 §4).
_LADDER_FOUND_DB = -6.0


def _ladder_run(fakes, manifest, door, verdict, captures):
    """A rung's run whose door finds the ladder's level at its first rung, whose
    probe banks the program it played, and whose probe reads 76 dB."""
    bank = manifest.bank

    async def probe_bank(record):
        if fakes.play.calls[-1]["spec"].level_probe:
            record = {**record, "program": build_summed_level_probe_program(
                (-24.0, -18.0, -12.0), sweep_band_hz=(20.0, 20000.0), gap_s=0.5,
                downstream_gain_db=record["level_db"]).to_dict()}
        return await bank(record)

    manifest.bank = probe_bank
    probed = TakeVerdict(False, next="retake_quieter", next_gain_db=-18.0, evidence={"level_db_spl": 76.0})
    return LevelRun(manifest, door, _analysis,
                    lambda *_args, **kwargs: probed if kwargs["pose_level"] is not None else verdict, captures)


def _finds_its_level(door, plan):
    door.caps_dbfs = {"woofer": 0.0, "tweeter": -6.0} if plan.level.level_db is None else None
    return door


@pytest.mark.parametrize("partial", [False, True, "last", "all", "stop"])
async def test_bass_levels_keep_one_hold_and_finish_each_pose(tmp_path, box, partial, rung_spl):
    """A ladder holds the room once and finishes each pose. Its first rung
    probes and finds its level, and each rung plays its step under it at every
    pose, a stated ladder too (ADR-0403 §4)."""
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    request = _walk([0, 20], candidates=("base",))
    request = replace(request, stops=tuple(replace(stop, purpose="bass", purposes=("bass",)) for stop in request.stops))
    ladder = preflight_levels(replace(request, levels=(-28.0, -23.0, -18.0)), ready_facts(request))
    fakes, gate, manifests = FakeSeams(), AnsweredGate(), []
    packet = RoundPacket(RunManifest("ladder", _Store(fakes.records)), ladder.to_dict())
    entry_volume = box.volume_db

    def prepare(plan):
        assert fakes.graph.restores == 0
        manifest = RunManifest(f"run-{len(manifests)}", packet)
        manifests.append(manifest)
        door = _finds_its_level(_run_door(tmp_path, box, fakes, manifest), plan)
        verdict = (TakeVerdict(False, fault=REASON_SPL_CEILING_EXCEEDED, next="stop") if partial == "stop" else
                   TakeVerdict(False, fault=REASON_CLIPPED, next="fix_and_retake")
                   if partial and (partial == "all" or len(manifests) == (6 if partial == "last" else 1)) else TakeVerdict(True))
        return _ladder_run(fakes, manifest, door, verdict, _summed_captures(plan))

    hold = _run_door(tmp_path, box, fakes, RunManifest("unused", _Store(fakes.records))).hold
    signals = plan_run.RunSignals()
    results = await run_levels(ladder, hold=hold, prepare=prepare, gate=gate, aborts=_ABORTS, signals=signals)
    await packet.finish()
    document = packet.to_dict()
    rungs = [(0, _LADDER_FOUND_DB)] if partial == "stop" else [
        (pose, _LADDER_FOUND_DB + step) for pose in (0, 20) for step in (0.0, -5.0, -10.0)]
    assert [(call["position_deg"], call["level_db"]) for call in fakes.play.calls] == [(0, 0.0), *rungs]
    assert fakes.play.calls[0]["spec"].level_probe
    assert len(gate.grants) == (1 if partial == "stop" else 2)
    assert sum(result.mic_moves for result in results) == len(gate.grants)
    assert all(result.finalized for result in results)
    statuses = ["partial" if partial and (partial == "all" or index == (5 if partial == "last" else 0)) else "complete"
                for index in range(len(rungs))]
    assert [run["status"] for run in document["runs"]] == statuses
    assert document["status"] == ("partial" if partial in ("all", "stop") else "complete")
    issues = document["schedule"]["issues"]
    assert len(issues) == statuses.count("partial")
    assert all(issue["blocking"] is False for issue in issues)
    if partial:
        reason = REASON_SPL_CEILING_EXCEEDED if partial == "stop" else REASON_CLIPPED
        refused = document["runs"][-1 if partial == "last" else 0]
        assert issues[0]["code"] == refused["reason"] == reason
        assert refused["not_measured"][0]["reason"] == reason
    assert signals.stop.is_set() is (partial == "stop")
    if partial == "stop":
        assert signals.stop_reason == REASON_SPL_CEILING_EXCEEDED
    assert len({result.run_id for result in results}) == len(rungs)
    assert fakes.graph.restores == 1
    assert box.volume_db == entry_volume


@pytest.mark.parametrize("purpose", ["room", "speaker"])
async def test_pilot_floor_keeps_take_and_packet_evidence(tmp_path, purpose):
    program = _conductor(FlowSeams()).program_for_phase("verify")
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
    manifest.begin({"index": 1, "repeat": 1, "pose": {"kind": "bearing", "azimuth_deg": 0}}, attempt=1, pose_index=0)
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


async def test_room_plan_levels_keep_pose_order(tmp_path, box, tuning_profile, rung_spl):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    candidate = _room_candidate(tuning_profile)
    program = run_preset("room")
    levels = (-10.0, -20.0)
    rung_spl[-20] = {"loudest_half_second_db_spl": 73, "max_window_db_spl": 73, "ceiling_db_spl": 95}
    request = ac.request_for_preset(program, candidates=("base", candidate.fingerprint), levels=levels)
    report = preflight_levels(request, ready_facts(request, candidates={candidate.fingerprint: candidate}, commissioning_stop_db_spl=95))
    assert not report.blocking
    fakes, gate, manifests = FakeSeams(), AnsweredGate(), []

    def prepare(plan):
        manifest = RunManifest(f"run-{len(manifests)}", _Store(fakes.records))
        manifests.append(manifest)
        return _ladder_run(fakes, manifest, _finds_its_level(_run_door(tmp_path, box, fakes, manifest), plan),
                           TakeVerdict(True), prepare_level_captures(plan))

    assert sum(len(row.schedule) for row in report.levels) == 3 * 2 * len(levels)
    hold = _run_door(tmp_path, box, fakes, RunManifest("unused", _Store(fakes.records))).hold
    results = await run_levels(report, hold=hold, prepare=prepare, gate=gate, aborts=_ABORTS)
    expected = [(pose.seat_offset_m, _LADDER_FOUND_DB + step, cid) for pose in program.poses
                for step in (0.0, -10.0) for cid in ("banked-base", candidate.fingerprint)]
    probe, *banked = [(row["seat_offset_m"], row["level_db"], row["candidate_id"]) for row in fakes.records.banked]
    assert (banked, probe) == (expected, (program.poses[0].seat_offset_m, 0.0, "banked-base"))
    assert len(fakes.play.calls) == len(expected) + 1
    assert len(gate.grants) == 3 and fakes.graph.restores == 1
    assert all(result.status == "complete" for result in results)


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
    request = (replace(_walk([0], ("fp-a", "fp-b", "fp-c", "fp-d")), repeats=2) if trial == 8 else
               _walk([0, -20, 20], ("fp-a", "fp-b", "fp-c")) if trial == 9 else _walk([0, -20, 20]))
    counts = [8] if trial == 8 else [3, 3, 3] if trial == 9 else [1, 1, 1]
    captures = plan_run.prepare_plan_captures(request)
    program = SimpleNamespace(phase="measure", sample_rate_hz=1, segments=(), stimulus_segments=lambda: segments)
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
    assert live[0]["estimated_seconds"] == sum(counts) * len(roles) * 4 + len(counts) * plan_run.HUMAN_MOVE_ALLOWANCE_S
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
    probe (ADR-0365, ADR-0403 §3)."""
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

    take_s = sum(segment.n_samples for segment in take.stimulus_segments()) / take.sample_rate_hz
    assert facts["estimated_seconds"] == pytest.approx(5 * take_s + 3 * probe.total_samples / probe.sample_rate_hz)


@pytest.mark.parametrize("repeats, counts, timing, preparation", [(1, [15, 8, 8], 1, 12), (2, [26, 16, 16], 2, 20)])
def test_three_pose_preview_counts_preparation_and_timing(repeats, counts, timing, preparation):
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    request = ac.request_for_preset(run_preset("tournament", "tournament_full"), repeats=repeats)
    captures = plan_run.prepare_plan_captures(request, roles_bands=context.roles_bands)
    facts = plan_run.preview_schedule(request, captures, context)
    assert facts["measurements"] == len(captures) == walk_price(request, roles_bands=context.roles_bands)["captures"]
    assert sum(facts["measurements_per_pose"]) == len(captures)
    assert facts["sweeps_per_pose"] == counts
    assert facts["timing_sweeps"] == timing
    assert facts["preparation_sweeps"] == preparation
    assert facts["sweeps"] == sum(counts)
    timing_rows = [row for row in facts["pose_sweeps"][0] if row["kind"] == "summed_sweep"]
    assert [(row["repeat"], row["repeats"]) for row in timing_rows] == [(n, repeats) for n in range(1, repeats + 1)]


def _ladder_execute(monkeypatch, box, levels, *, manifest=None, production=None):
    """A one-rung ladder host's ``execute``, with ``levels`` standing in for ``run_levels``."""
    monkeypatch.setattr(correction_run_host, "bind_plan_analysis", lambda *a, **kw: (None, None))
    monkeypatch.setattr(correction_run_host, "resolved_household_sensitivity", lambda _: None)
    monkeypatch.setattr(correction_run_host, "run_levels", levels)
    return correction_run_host.bind_run_door(
        host=None, device=None, evidence_store=None, manifest=RunManifest("packet", _Store(FakeSeams().records)) if manifest is None else manifest,
        production=FakeSeams() if production is None else production, conductor=None, refs={}, trims={}, ceiling_s=30, ceiling_db_spl=85,
        camilla_factory=lambda: box,
        ladder=SimpleNamespace(admissible=[None], plan=SimpleNamespace(levels=(-23,)), to_dict=lambda: {}),
    )[3]


async def test_a_ladder_ends_on_the_counts_its_banked_manifest_prints(monkeypatch, box):
    """The ladder's last published facts count from the joined manifest that
    ``wait`` reprints once banked, so the two "Measured" lines agree."""
    joined = {"status": "complete", "reason": "", "level": {}, "runs": [], "honoured": {"retakes": 0},
              "sets": [{"takes": [{"take_id": "t1", "selected": True}, {"take_id": "t2", "selected": False}]}],
              "not_measured": [{"pose": {"azimuth_deg": 0}, "reason": "summed_sweep_heard"}] * 3}
    packet = SimpleNamespace(runs={}, to_dict=lambda: joined, finish=AsyncMock(), update_schedule=AsyncMock())
    monkeypatch.setattr(correction_run_host, "RoundPacket", lambda *_args: packet)
    gate = AnsweredGate()
    execute = _ladder_execute(monkeypatch, box, AsyncMock(return_value=[]))
    await execute(None, gate=gate, signals=plan_run.RunSignals(), captures=())
    ended = gate.progress[-1]
    assert (ended["takes"], ended["not_measured"]) == (1, 3)
    assert round_lines(ended)[0] == coverage_lines({}, joined)[0]


async def test_a_ladder_stopped_on_the_channel_map_keeps_the_drivers_it_named(monkeypatch, box):
    child = RunManifest("packet-level-1", _Store(FakeSeams().records))
    child.failed_roles = ("tweeter",)

    async def levels(_ladder, *, signals, **_kwargs):
        signals.request_stop(REASON_CHANNEL_MAP_MISMATCH)
        return (child,)

    execute = _ladder_execute(monkeypatch, box, levels)
    result = await execute(None, gate=AnsweredGate(), signals=plan_run.RunSignals(), captures=())
    assert (result.reason, result.failed_roles) == (REASON_CHANNEL_MAP_MISMATCH, ("tweeter",))


@pytest.mark.parametrize("muted", [False, True], ids=["over_limits", "output_muted"])
@pytest.mark.parametrize("site", ["transaction", "executor", "ladder"])
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
    outer = RunManifest("packet", store)
    gate = AnsweredGate()
    if site == "ladder":
        execute = _ladder_execute(monkeypatch, box, AsyncMock(side_effect=failure), manifest=outer, production=fakes)
        with pytest.raises(ProgramPlaybackRefused):
            await execute(None, gate=gate, signals=plan_run.RunSignals(), captures=())
    else:
        packet = RoundPacket(outer, {})
        manifest = RunManifest("run", packet)
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
        run = plan_run.run_plan(request, door=door, manifest=manifest, analyze=_analysis, gate=gate, aborts=_ABORTS,
                                captures=_summed_captures(request))
        if site == "executor":
            with pytest.raises(ProgramPlaybackRefused):
                await run
        else:
            await run
            play.assert_not_awaited()
        await packet.finish()
        assert packet.runs["run"]["reason"] == reason
        assert all(row["reason"] == reason for row in manifest.not_measured)
    saved = store.snapshots[-1]
    assert saved["reason"] == reason
    if site == "transaction":
        never_played, = (take for group in saved["sets"] for take in group["takes"])
        assert never_played == {"take_id": never_played["take_id"], "record_id": "", "selected": False}
        assert with_records(tmp_path, saved, every_take=True)["sets"] == saved["sets"]
        stop = saved["not_measured"][0]
        assert (stop["fault"], stop["evidence"]["admission"]) == (reason, admission.to_dict())
