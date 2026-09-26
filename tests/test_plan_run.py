# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The executor's observable work, placement, evidence and control contract."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from itertools import count
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

from jasper.active_speaker import angle_capture as ac, plan_run
from jasper.active_speaker.excitation_safety_plan import resolve_driver_excitation_ceilings
from jasper.active_speaker.run_levels import LevelRun, level_ladder, preflight_levels, prepare_level_captures, run_levels
from jasper.active_speaker.measurement_programs import (
    MeasurementProgram, ProgramPose, run_program, program as measurement_program,
)
from jasper.active_speaker.crossover_v2 import capture_dispatch
from jasper.active_speaker.crossover_v2.admission import MAX_AUTOMATIC_RETAKES_PER_POSITION, MAX_EXTRA_ATTEMPTS_PER_POSITION
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_CANDIDATE, POSITION_AXIS_VERTICAL
from jasper.active_speaker.crossover_v2.position_gate import POSITION_HOLD_EXPIRED_CODE, PositionGate
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY, REASON_DRIFT_BASELINES_DISAGREE, REASON_CLIPPED, REASON_ANCHOR_AMBIGUOUS,
    REASON_SPL_CEILING_EXCEEDED, TakeVerdict,
)
from jasper.active_speaker.program_admission import ProgramAdmission, ProgramAdmissionRefusal, SegmentAdmission
from jasper.active_speaker.program_playback import ProgramPlaybackRefused
from jasper.active_speaker.crossover_v2.program_transaction import ProgramForStimulus, ProgramPlaybackTransaction
from jasper.active_speaker.run_manifest import RunManifest, RUN_MANIFEST_KIND, TAKE_INCOMPLETE
from jasper.active_speaker.round_packet import RoundPacket, write_round_packet
from jasper.active_speaker.round_copy import PLACE_MICROPHONE, coverage_lines, round_lines
from jasper.active_speaker.capture_provenance import stimulus_peak_dbfs
from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.level import LevelReading
from jasper.audio_measurement.program import ExcitationProgram, RoleBand, build_level_probe_program, build_measure_program
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from jasper.volume_owner import ClaimKind, volume_owner
from jasper.web import correction_run_host
from tests.crossover_v2_fixtures import (
    FakeSeams as FlowSeams, _conductor, _loc, _measure_analysis, _verify_analysis, _roles,
)
from tests.crossover_v2_banked_round import bank_seat_round
from tests.engine_twin import FakeGraph, FakeSeams, FakePlay, SeamFailure, open_session
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_preflight import ready_facts
from tests.test_active_speaker_measurement_door import box as box  # noqa: F401
from tests.test_crossover_v2_tuning_scope import _room_candidate, tuning_profile as tuning_profile

_ABORTS = {SeamFailure: "seam_failed"}

def _walk(angles, candidates=("fp-a",)):
    return ac.AngleCaptureRequest(candidates=candidates, stops=tuple(
        ac.AngleStop(angle, ac.REGIME_SUMMED, candidate_id=candidate)
        for angle in angles for candidate in candidates),
        template=ac.walk_template(kind=MEASURE_KIND_CANDIDATE))


def _analysis(_record, _record_id):
    return ProgramAnalysis(phase="verify", program_id="test", locations=(_loc("sweep"),))


def fake_program_baselines(monkeypatch):
    monkeypatch.setattr("jasper.active_speaker.candidate_parts.baseline_candidate_id",
                        lambda: "banked-base")


@pytest.fixture(autouse=True)
def banked_program_baselines(monkeypatch):
    fake_program_baselines(monkeypatch)


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


async def _run_gated(request, *, seams=None, gate=None, analyze=_analysis, signals=None, captures=None, **kwargs):
    fakes = seams or FakeSeams()
    manifest = RunManifest("run", _Store(fakes.records))
    async with open_session(replace(fakes, records=manifest), allocate_take_id=manifest.allocate_take_id) as (session, _):
        result = await plan_run.run_plan(request, session=session, manifest=manifest, analyze=analyze,
                                         gate=gate, aborts=_ABORTS, signals=signals, captures=captures, **kwargs)
    return result, fakes


def _takes(document):
    return [take for group in document["sets"] for take in group["takes"]]


@pytest.mark.parametrize(("purpose", "record_fields", "expected"), [
    ("room", {}, {"pose_kind": "bearing", "mark_distance_m": 1.0,
                  "seat_offset_m": None, "measurement_purpose": "room"}),
    (None, {}, {"pose_kind": "bearing", "mark_distance_m": 1.0,
               "seat_offset_m": None, "measurement_purpose": "speaker"}),
    (None, {"pose_kind": "seat", "mark_distance_m": None,
            "seat_offset_m": [0.2, 0.0, 0.1], "measurement_purpose": "room"},
     {"pose_kind": "seat", "mark_distance_m": None,
      "seat_offset_m": [0.2, 0.0, 0.1], "measurement_purpose": "room"}),
])
def test_manifest_banks_resolved_measurement_purpose(purpose, record_fields, expected):
    records = FakeSeams().records
    manifest = RunManifest("run", records)
    stop = {"index": 1, "repeat": 1, "pose": {"kind": "bearing", "distance_m": 1.0}}
    if purpose is not None:
        stop["purpose"] = purpose
    manifest.begin(stop, attempt=1, pose_index=0)

    asyncio.run(manifest.bank({"take_id": "take", **record_fields}))

    assert {key: records.banked[0][key] for key in expected} == expected


@pytest.mark.parametrize(("angles", "candidates"), [([0], ("fp-a",)), ([0, 20], ("fp-a", "fp-b")), ([0, -20, 20], ("fp-a",))])
@pytest.mark.parametrize("repeats", [1, 2, 3])
def test_a_walk_groups_configs_and_repeats_under_one_pose_grant(angles, candidates, repeats):
    request, gate = replace(_walk(angles, candidates), repeats=repeats), AnsweredGate()
    ac.session_lateral_walk(request, externally_positioned=False, base_entries=0, supported_summed_candidates=True)
    captures = plan_run.prepare_plan_captures(request)
    result, fakes = asyncio.run(_run_gated(request, gate=gate))
    assert result.status == "complete"
    assert len(result.wall_s) == result.mic_moves == len(gate.grants) == len(angles)
    assert result.takes_measured == len(fakes.banked) == len(captures) == len(angles) * len(candidates) * repeats
    assert fakes.graph.scopes == [("candidate", c.spec.candidate_id) for c in captures]
    assert fakes.graph.restores == fakes.volume.releases == 1
    doc = json.loads(json.dumps(result.to_dict()))
    assert len(doc["sets"]) == len(set(candidates))
    for group in doc["sets"]:
        assert [(t["pose"]["deg"], t["repeat"], t["selected"]) for t in group["takes"]] == [
            (angle, repeat, True) for angle in angles for repeat in range(1, repeats + 1)]
    assert len({t["take_id"] for t in _takes(doc)}) == len(captures)
    assert (doc["not_measured"], doc["honoured"]["takes_refused"]) == ([], 0)
    assert all(row["budget"]["by_household"] == row["budget"]["by_speaker"] == 0 for row in gate.progress)


def test_skipped_per_driver_work_is_disclosed_without_an_extra_grant():
    request = ac.AngleCaptureRequest(candidates=("fp-a", "base", "fp-b"), stops=(
        ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-a"),
        ac.AngleStop(0, ac.REGIME_PER_DRIVER), ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-b")))
    gate = AnsweredGate()
    result, _ = asyncio.run(_run_gated(request, gate=gate))
    assert result.status == "partial"
    assert (len(result.planned), result.takes_measured, len(result.not_measured)) == (3, 2, 1)
    assert gate.grants == [(1, 1)]
    assert result.not_measured[0]["reason"] == ac.WALK_NOTHING_PLAYABLE


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


@pytest.mark.parametrize("plan", [
    ac.AngleCaptureRequest(candidates=("fp-a",), stops=(ac.AngleStop(20, ac.REGIME_SUMMED, candidate_id="fp-a"),),
        template=ac.walk_template(kind=MEASURE_KIND_CANDIDATE, position_axis=POSITION_AXIS_VERTICAL)),
    ac.per_driver_at([0]),
])
def test_unplayable_plan_records_each_missing_stop(plan):
    result, fakes = asyncio.run(_run_gated(plan))
    assert result.status == "partial"
    assert result.reason in {ac.WALK_STIMULUS_NOT_ACCEPTED, ac.WALK_NOTHING_PLAYABLE}
    assert len(result.not_measured) == len(plan.stops)
    assert fakes.play.calls == []


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
    rows = _takes(result.records.snapshots[-1])
    assert len({(t["index"], t["stimulus_ordinal"]) for t in rows}) == 1
    assert [t["attempt"] for t in rows] == ([1, 2] if refusal else [1])
    assert [t["selected"] for t in rows] == ([False, True] if refusal else [True])
    assert [{key: t[key] for key in ("fault", "next", "charge") if key in t} for t in rows] == ([refusal, {}] if refusal else [{}])
    assert result.to_dict()["honoured"]["takes_refused"] == (0 if ok else 1)
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
    def analyze(record, record_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            getattr(signals, action).set()
        return _analysis(record, record_id)
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
    def analyze(record, record_id):
        seen.append(gate.published())
        return _analysis(record, record_id)
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
                                           gate=gate, aborts=_ABORTS, clock=lambda: now)
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
    for take in _takes(terminal):
        assert take["timing"] == {"started_s": 60.0, "ended_s": 65.0}
    assert event.call_args.kwargs["status"] == expected


@pytest.mark.parametrize("accepted", [True, False])
def test_done_requires_every_stimulus_at_the_last_stop(monkeypatch, accepted):
    signals = plan_run.RunSignals()
    verdicts = iter([TakeVerdict(True), TakeVerdict(accepted, None if accepted else REASON_CLIPPED,
                     next="accept" if accepted else "retake_same", charge="speaker")])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    request = _walk([0])
    request = replace(request, template=replace(request.template, level_ladder_dbfs=(-24, -18)))
    def analyze(record, record_id):
        signals.complete.set()
        return _analysis(record, record_id)
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
    def analyze(record, record_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            if action == "cancel":
                raise asyncio.CancelledError
            if action == "complete":
                signals.complete.set()
        return _analysis(record, record_id)
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
                         analyze=_analysis, gate=AnsweredGate(), aborts=_ABORTS))
    root = Path(info["bundle_dir"]) / "evidence/v1/artifacts"
    document = json.loads((root / result.path).read_text())
    records = [json.loads((root / take["artifacts"]["record_id"]).read_text()) for take in _takes(document)]
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


def test_failed_analysis_keeps_the_raw_record(monkeypatch):
    def broken(*args):
        raise ValueError("bad capture")
    result, fakes = asyncio.run(_run_gated(_walk([0]), analyze=broken))
    assert result.status == "partial"
    assert result.takes_measured == len(fakes.banked) == 1
    assert result.takes[0]["artifacts"]["record_id"]
    assert result.takes[0]["fault"] in REASON_REGISTRY


def test_real_assessor_sees_glitch_and_retries_once():
    calls = 0
    def analyze(record, record_id):
        nonlocal calls
        calls += 1
        return replace(_analysis(record, record_id), discontinuity_samples=1024 if calls == 1 else 0)
    result, fakes = asyncio.run(_run_gated(replace(_walk([0]), retries_per_pose=0), analyze=analyze))
    assert len(fakes.banked) == 2
    assert result.takes[0]["fault"] == REASON_DRIFT_BASELINES_DISAGREE
    assert result.status == "complete"


@pytest.mark.parametrize("changed", [
    {}, {"candidate_id": "candidate"}, {"graph_fingerprint": "other"}, {"program_id": "other"},
    {"level_db": -12.0}, {"stimulus_dbfs": -24.0}, {"loudness_volume_db": -30.0},
    {"capture_calibration": {"applied": True, "calibration_id": "other", "curve_fingerprint": "curve"}},
    {"regime": "other"}, {"side": "right"}, {"role": "tweeter"},
])
def test_manifest_set_identity_tracks_capture_basis_and_spans_poses(changed):
    manifest = RunManifest("run", _Store(FakeSeams().records))
    record = {"candidate_id": "", "graph_fingerprint": "graph", "program_id": "program",
              "level_db": -20.0, "stimulus_dbfs": -18.0, "loudness_volume_db": -20.0,
              "regime": "summed", "side": "left", "role": "summed"}
    async def append():
        for index, degrees in enumerate([0, 10, 20], 1):
            manifest.begin({"index": index, "repeat": 1, "pose": {"deg": degrees}}, attempt=1, pose_index=index - 1)
            await manifest.append({**record, "take_id": manifest.allocate_take_id(), **(changed if index == 2 else {})}, f"record-{index}",
                                  TakeVerdict(True), complete=True, started_s=index, ended_s=index + 1, level_observation={})
    asyncio.run(append())
    groups = manifest.to_dict()["sets"]
    assert len(groups) == (2 if changed else 1)
    assert {t["pose"]["deg"] for t in groups[0]["takes"]} == ({0, 20} if changed else {0, 10, 20})


def test_manifest_names_emitted_role_levels_and_usable_bands():
    manifest = RunManifest("run", _Store(FakeSeams().records))
    manifest.begin({"index": 1, "repeat": 1, "pose": {"deg": 0}}, attempt=1, pose_index=0)
    record = {"take_id": manifest.allocate_take_id(), "stimulus_dbfs": -12, "program": {"segments": [
        {"kind": "pilot", "role": "pilot_only", "gain_db": -6},
        {"kind": "sweep", "role": "woofer", "gain_db": -18},
        {"kind": "sweep", "role": "tweeter", "gain_db": -24},
    ]}, "curves": [{"role": "woofer", "band_hz": [20, 2000], "validity_floor_hz": 100}]}
    asyncio.run(manifest.append(record, "record", TakeVerdict(True), complete=True, started_s=0, ended_s=1, level_observation={}))
    groups = manifest.to_dict()["sets"]
    assert {group["capture_basis"]["role"] for group in groups} == {"woofer", "tweeter"}
    assert {group["capture_basis"]["role"]: group["takes"][0]["level"]["stimulus_dbfs"]
            for group in groups} == {"woofer": -18, "tweeter": -24}
    assert next(group["takes"][0]["quality"]["usable_band_hz"] for group in groups
                if group["capture_basis"]["role"] == "woofer") == [100, 2000]
    assert manifest.takes_measured == 1


def test_interrupted_spec_keeps_its_planned_index_after_a_skipped_stop():
    request = ac.AngleCaptureRequest(candidates=("base", "fp-a"), stops=(
        ac.AngleStop(0, ac.REGIME_PER_DRIVER), ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-a")))
    result, _ = asyncio.run(_run_gated(request, seams=FakeSeams(graph=_StoppingGraph(stop_after=1))))
    assert result.stopped_at["index"] == 2
    assert result.specs[result.stopped_at["index"]].candidate_id == "fp-a"


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
    result = await plan_run.run_plan(replace(_walk([0]), level=ac.LevelPolicy(resolved=ac.ResolvedLevel(75, -14, "1234"))),
                                      door=door, manifest=manifest, analyze=_analysis,
                                      aborts=_ABORTS)
    assert (result.status, result.reason, result.takes_measured) == ("partial", reason, 0)
    build.assert_not_called()
    assert not graph.installs


@pytest.mark.parametrize("layout,poses", [("baseline/express", 5), ("baseline/full", 13)])
def test_baseline_pairs_driver_and_room_reads_and_keeps_timing_at_entry(layout, poses):
    program = run_program("speaker", layout)
    request = ac.request_for_program(program, repeats=2)
    captures = plan_run.prepare_plan_captures(request)
    timing = [capture for capture in captures if capture.spec.graph_scope == "timing"]
    assert [(capture.stop.angle_deg, capture.repeat) for capture in timing] == [(0, 1), (0, 2)]
    room = [capture for capture in captures if capture.stop.purpose == "room"]
    assert len(room) == poses * 2
    assert {capture.stop.place for capture in room} == {pose.place for pose in program.poses}
    assert {(capture.spec.program_phase, capture.spec.graph_scope) for capture in room} == {("lateral", "candidate")}
    assert all(capture.resolved(request).prompt.purpose == "room" for capture in room)
    assert [capture.stop.place for capture in captures if capture.spec.program_phase == "measure"] == [
        pose.place for pose in program.poses for _ in range(pose.repeats * 2)]


def test_a_hand_written_branch_plan_resolves_its_base_entry_as_a_summed_take():
    plan = ac.AngleCaptureRequest(
        (ac.AngleStop(0, ac.REGIME_BRANCHES, branch_pair="front_rear"),),
    )
    request = ac.AngleCaptureRequest.from_mapping(json.loads(json.dumps(plan.to_dict())))
    captures = plan_run.prepare_plan_captures(request, roles_bands=tuple(_roles()))

    entry = next(c for c in captures if c.spec.program_phase == "entry_baseline")
    assert (entry.stop.regime, entry.stop.branch_pair) == (ac.REGIME_SUMMED, "drivers")
    assert entry.spec.branch_target_ids == ()
    assert {c.spec.branch_target_ids for c in captures if c.spec.graph_scope == "candidate_branches"} == {
        ("woofer", "woofer:rear")}


def test_speaker_room_layout_pairs_driver_and_summed_stops_with_entry_timing():
    program = run_program("speaker", "room_quick")
    request = ac.request_for_program(program, mover=ac.MOVER_ARM)
    _, safety, targets = _profile_and_targets(woofer_floor=30)
    roles = tuple(RoleBand(role, channel, resolve_driver_excitation_ceilings(
        safety, fingerprint, program_admission=True)[0])
        for channel, (role, fingerprint) in enumerate(targets.items()))

    assert [(stop.regime, stop.purpose) for stop in request.stops] == [
        pair for _pose in program.poses
        for pair in [(ac.REGIME_PER_DRIVER, "speaker"), (ac.REGIME_SUMMED, "room")]
    ]
    captures = plan_run.prepare_plan_captures(request, roles_bands=roles)
    room_capture = next(capture for capture in captures if capture.stop.purpose == "room")
    room_band = ac.room_sweep_band_hz(
        roles, (room_capture.resolved(request).prompt,)
    )
    assert room_band == (20.0, 20000.0)
    assert {capture.spec.sweep_band_hz for capture in captures if capture.stop.purpose == "room"} == {room_band}
    assert {capture.spec.sweep_band_hz for capture in captures if capture.stop.purpose == "speaker"} == {()}
    timing = [capture for capture in captures
              if capture.spec.graph_scope == "timing"]
    assert [(capture.stop.angle_deg, capture.spec.program_phase) for capture in timing] == [
        (0, "entry_baseline")]


@pytest.mark.parametrize(("regime", "candidate", "purpose", "phases", "scope"), [
    ("per_driver", "base", "speaker", ("check", "entry_baseline", "measure"), "timing"),
    ("summed", "base", "speaker", ("entry_baseline", "lateral"), "timing"),
    ("summed", "candidate-a", "speaker", ("lateral",), None),
    ("summed", "base", "room", ("lateral",), None),
    ("summed", "base", "rear", ("lateral",), None),
    ("summed", "base", "bass", ("lateral",), None),
    ("summed", "base", "reference", ("lateral",), None),
])
@pytest.mark.parametrize("repeats", [1, 3])
def test_inline_plan_derives_only_the_preparation_it_needs(regime, candidate, purpose, phases, scope, repeats):
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(20, regime, candidate_id=candidate, purpose=purpose),),
        candidates=(candidate,), repeats=repeats,
    )
    captures = plan_run.prepare_plan_captures(request)
    phase_repeats = {"check": 1, "entry_baseline": repeats, "measure": repeats, "lateral": repeats}
    assert tuple(capture.spec.program_phase for capture in captures) == tuple(
        phase for phase in phases for _ in range(phase_repeats[phase]))
    assert [capture.repeat for capture in captures[-repeats:]] == list(range(1, repeats + 1))
    assert [capture.stop.angle_deg for capture in captures[-repeats:]] == [20] * repeats
    assert all(capture.stop.angle_deg == 0 for capture in captures[:-repeats])
    baseline = [capture for capture in captures if capture.spec.program_phase == "entry_baseline"]
    assert [capture.repeat for capture in baseline] == (list(range(1, repeats + 1)) if scope else [])
    assert all((capture.spec.graph_scope, capture.stop.regime, capture.spec.positions, capture.spec.vertical_deg)
               == (scope, "summed", (0,), 0) for capture in baseline)
    assert all(capture.spec.graph_scope == ("drivers" if regime == "per_driver" else "candidate")
               for capture in captures[-repeats:])


def test_a_near_field_plan_asks_for_every_driver_pose_and_banks_reference_takes():
    """Each pose plays its own driver alone, the gate asks for the microphone
    at every pose (the front and rear woofer at one distance included), and
    every take banks as reference evidence at its driver (ADR-0360)."""
    layout = [(driver, mm) for driver in ("woofer", "woofer:rear") for mm in (15, 30, 15)]
    program = MeasurementProgram("nearfield", "custom", tuple(
        ProgramPose(0, 0, kind="close", distance_m=mm / 1000, driver=driver) for driver, mm in layout),
        purpose="reference", regime="near_field")
    request = ac.AngleCaptureRequest.from_mapping(json.loads(json.dumps(ac.request_for_program(program).to_dict())))
    captures = plan_run.prepare_plan_captures(request)
    gate = AnsweredGate()

    result, fakes = asyncio.run(_run_gated(request, gate=gate, captures=captures,
                                           assessor=lambda *_args, **_kwargs: TakeVerdict(True, next="accept")))

    assert [(c.spec.graph_scope, c.spec.branch_target_ids, c.spec.regime, c.spec.program_phase) for c in captures] == [
        ("drivers", (driver,), "near_field", "lateral") for driver, _ in layout]
    assert result.status == "complete"
    assert len(gate.grants) == result.mic_moves == len(layout)
    assert [(take["measurement_purpose"], take["pose_driver"], take["mark_distance_m"]) for take in fakes.banked] == [
        ("reference", driver, mm / 1000) for driver, mm in layout]


_MIC = MicSensitivity(-12.0)


class _LevelStore(_Store):
    """Banks each take as the web host does: the program it played, at the
    peak it asked for under its ceiling, and what the microphone read."""

    def __init__(self, records, readings, probe_db, ceiling_db):
        super().__init__(records)
        self.readings, self.probe_db, self.ceiling_db = iter(readings), probe_db, ceiling_db

    async def bank(self, record):
        if record.get("kind") != RUN_MANIFEST_KIND:
            band = RoleBand("woofer", 0, FrequencyBand(20, 2000))
            peak = min(self.probe_db if record.get("stimulus_dbfs") is None else record["stimulus_dbfs"], self.ceiling_db)
            reading = next(self.readings)
            program = (build_level_probe_program(band, (peak,), sweep_band_hz=(20.0, 2000.0), gap_s=0.5,
                                                 downstream_gain_db=0.0, channels=1)
                       if record.get("stimulus_dbfs") is None and record.get("pose_driver") else
                       build_measure_program({"woofer": peak}, (band,), repeat_count=1, sweep_durations={"woofer": 0.2}))
            record.update(
                program=program.to_dict(),
                capture_integrity={"spl": {"max_window_db_spl": reading, "loudest_half_second_db_spl": reading - 3,
                                           "ceiling_db_spl": 85.0, "sens_factor_db": _MIC.sens_factor_db}})
        return await super().bank(record)


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


def _heard_analysis(record, _record_id):
    """The play's located sweeps read what the microphone heard, 30 dB over the room (ADR-0364)."""
    program = ExcitationProgram.from_dict(record["program"])
    heard = _MIC.dbfs_from_db_spl(record["capture_integrity"]["spl"]["max_window_db_spl"])
    return replace(_measure_analysis(program),
                   stimulus_levels=(LevelReading(stimulus_peak_dbfs(program), heard, heard - 30.0),))


def _run_levelled(request, readings, *, replace_at=None, ceiling_db=0.0, redo_at=()):
    """A plan whose recordings pass, admitted by the conductor as a web run's are
    and judged on the level each take read; the microphone is re-placed at take
    ``replace_at``, and the operator presses Redo during each take in
    ``redo_at``, take 0 being just after the first placement is confirmed."""
    fakes, takes, signals = FakeSeams(), count(1), plan_run.RunSignals()
    gate = _RedoOnPlacementGate(signals) if 0 in redo_at else AnsweredGate()
    manifest = RunManifest("run", _LevelStore(fakes.records, readings, probe_db=-42.0, ceiling_db=ceiling_db))
    captures = plan_run.prepare_plan_captures(request)
    conductor = _conductor(FlowSeams(), index_phase_map={i: c.spec.program_phase for i, c in enumerate(captures, 1)})

    def assessor(analysis, **kwargs):
        take = next(takes)
        if take in redo_at:
            signals.retake.set()
        if take == replace_at:
            return TakeVerdict(False, next="fix_and_retake", charge="operator")
        return capture_dispatch.assess(analysis, **kwargs)

    async def run():
        async with open_session(replace(fakes, records=manifest), allocate_take_id=manifest.allocate_take_id) as (
                session, _):
            return await plan_run.run_plan(
                request, session=session, manifest=manifest, gate=gate, aborts=_ABORTS, signals=signals,
                analyze=_heard_analysis, captures=captures, assessor=assessor,
                admit=lambda i, a, e, ledger: conductor.authorize_begin(i, a, e, executor_ledger=ledger))

    result = asyncio.run(run())
    selected = [take["selected"] for take in sorted(_takes(result.to_dict()), key=lambda take: take["take_id"])]
    return result, fakes, selected, gate


def test_a_near_field_take_levels_itself_before_it_is_kept():
    """Each placement's first attempt plays under the target and is retaken at
    the solved peak; the rest of that placement starts there, a re-placement
    starts with its probe again, and in-band re-seats are never
    sent back as drift, though each banks its reading (ADR-0361)."""
    request = ac.request_for_program(MeasurementProgram("nearfield", "custom", tuple(
        ProgramPose(0, 0, repeats=repeats, kind="close", distance_m=mm / 1000, driver="woofer")
        for mm, repeats in ((15, 2), (30, 1), (15, 1))), purpose="reference", regime="near_field"))
    readings = (66.0, 79.0, 81.0, 66.0, 80.0, 64.0, 79.0, 66.0, 82.0)

    result, fakes, selected, gate = _run_levelled(request, readings, replace_at=3)

    assert result.status == "complete"
    assert fakes.play.rungs == [None, -29.0, -29.0, None, -29.0, None, -27.0, None, -29.0]
    assert selected == [False, True, False, False, True, False, True, False, True]
    steps = {(p["measurement"], p["attempt"]): p["level_step"] for p in gate.progress if "level_step" in p}
    assert [step == "probe" for step in steps.values()] == [rung is None for rung in fakes.play.rungs]
    assert [take["level"]["loudest_half_second_db_spl"] for take in sorted(
        _takes(result.to_dict()), key=lambda take: take["take_id"])] == [reading - 3 for reading in readings]


def test_a_near_field_take_its_ceiling_holds_quiet_is_kept_not_retaken():
    """A take its ceiling played under the peak it asked for is kept too quiet:
    a louder retake would replay it until the pose's retries ran out (ADR-0361)."""
    request = ac.request_for_program(MeasurementProgram("nearfield", "custom", (
        ProgramPose(0, 0, kind="close", distance_m=0.03, driver="woofer"),), purpose="reference", regime="near_field"))

    result, fakes, selected, _ = _run_levelled(request, (66.0, 77.0), ceiling_db=-30.0)

    assert result.status == "complete"
    assert (fakes.play.rungs, selected) == ([None, -29.0], [False, True])


def test_a_far_field_take_keeps_the_drift_rule_and_is_never_levelled():
    """Only a take at one driver's pose is held to the near-field target: a
    far-field repeat that reads 3 dB off its first is retaken as drift, at the
    same level (ADR-0361)."""
    result, fakes, selected, gate = _run_levelled(replace(_walk([0]), repeats=2), (70.0, 73.0, 70.0))

    assert result.status == "complete"
    assert fakes.play.rungs == [None, None, None]
    assert selected == [True, False, True]
    assert not any("level_step" in progress for progress in gate.progress)


@pytest.mark.parametrize("retries", [0, MAX_EXTRA_ATTEMPTS_PER_POSITION])
def test_a_redo_at_a_driver_pose_places_it_again_and_never_ends_the_round(retries):
    """Each redo asks for the microphone again and starts the pose over at its
    probe, with its retries, so redos past the pose's budget never end the
    round, even one with no retries; the page is told which plays are the
    probe, and a pose's takes play at the level its probe solved (ADR-0365)."""
    request = ac.request_for_program(MeasurementProgram("nearfield", "custom", tuple(
        ProgramPose(0, 0, repeats=repeats, kind="close", distance_m=mm / 1000, driver="woofer")
        for mm, repeats in ((15, 1), (30, 2))), purpose="reference", regime="near_field"), retries_per_pose=retries)
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


@pytest.mark.parametrize("redo_at,readings,allowed", [
    ((0,), (66.0, 80.0, 80.0), 0),
    ((3,), (66.0, 80.0, 80.0, 66.0, 80.0, 80.0), 2),
], ids=["before_any_take", "during_the_second_take"])
def test_a_redo_leaves_a_driver_pose_its_retries(monkeypatch, redo_at, readings, allowed):
    """Admission charges every attempt after a take's first. A redo before any
    take played only asks for the placement again, and a redo after two takes
    carries a retry for each take it plays again, so a pose with no retries
    still completes (ADR-0361)."""
    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    request = ac.request_for_program(MeasurementProgram("nearfield", "custom", (
        ProgramPose(0, 0, repeats=2, kind="close", distance_m=0.015, driver="woofer"),),
        purpose="reference", regime="near_field"), retries_per_pose=0)

    result, fakes, selected, gate = _run_levelled(request, readings, redo_at=redo_at)

    assert (result.status, result.reason, result.not_measured) == ("complete", "", [])
    assert [index for index, _ in gate.grants] == [1, 1]
    assert selected[-2:] == [True, True]
    assert [p["budget"]["allowed"] for p in gate.progress if "budget" in p][-1] == allowed


@pytest.mark.parametrize("purpose,layout,entry,poses", [
    ("speaker", "baseline/express", True, 5), ("room", "seat_express", False, 3),
    ("rear", "rear/express", False, 3),
])
def test_program_entry_baseline_and_placement_count(purpose, layout, entry, poses):
    request = ac.request_for_program(run_program(purpose, layout))
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    captures = plan_run.prepare_plan_captures(request, roles_bands=context.roles_bands)
    assert any(c.spec.program_phase == "entry_baseline" for c in captures) is entry
    assert plan_run.preview_schedule(request, captures, context)["poses"] == poses


async def test_room_uses_its_first_seat_take_as_the_level_reference():
    request = ac.request_for_program(run_program("room", "seat_express"))
    captures = plan_run.prepare_plan_captures(request)
    manifest = RunManifest("run", _Store(FakeSeams().records), program=request.program)
    for index, (capture, observed, accepted, action, delta) in enumerate(zip(
        captures, (70, 78), (True, False), ("accept", "retake_same"), (None, 8),
    ), 1):
        assert (capture.spec.program_phase, capture.stop.kind) == ("lateral", "seat")
        manifest.begin({"index": index, "pose": {"kind": "seat", "seat_offset_m": capture.stop.seat_offset_m},
                        "candidate_id": capture.stop.candidate_id}, attempt=1, pose_index=index - 1)
        record = {"take_id": str(index), "level_db": -20, "program_id": "room", "phase": "lateral",
                  "capture_integrity": {"spl": {"loudest_half_second_db_spl": observed}}}
        verdict = capture_dispatch.level_drift_verdict(**manifest.level_observation(record))
        assert (verdict.ok, verdict.next, verdict.evidence.get("level_delta_db")) == (accepted, action, delta)
        await manifest.append(record, str(index), verdict, complete=True, started_s=index, ended_s=index + 1,
                              level_observation=verdict.evidence)
    assert [take["selected"] for take in _takes(manifest.to_dict())] == [True, False]


@pytest.mark.parametrize(("requested", "level", "source"), [
    (None, -15, "seat_reference"), (-25, -25, "operator"), (0, 0, "operator"),
])
async def test_check_plays_at_the_session_level(tmp_path, box, requested, level, source):
    from tests.test_correction_crossover_v2_wired import _run_door

    fakes = FakeSeams()
    manifest = RunManifest("run", _Store(fakes.records))
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),),
        level=ac.LevelPolicy(level_db=requested, resolved=ac.ResolvedLevel(75, -15, "1234")),
        level_source=source,
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
    assert manifest.to_dict()["level"] == {"session": request.level.resolved.session(),
                                          "run": {"level_db": level, "offset_db": level + 15,
                                                  "level_source": source}}
    assert {row["capture_basis"]["level_db"] for row in manifest.to_dict()["sets"]} == {level}
    assert [(call["spec"].program_phase, call["level_db"]) for call in fakes.play.calls] == [
        (phase, level) for phase in ("check", "entry_baseline", "measure")]


async def test_manifest_discloses_program_default_without_a_seat_reference():
    result, _ = await _run_gated(_walk([0]))

    assert result.to_dict()["level"]["run"] == {
        "level_db": -20.0, "offset_db": None, "level_source": "program_default",
    }


@pytest.mark.parametrize("level", [-20, -25])
async def test_run_requires_the_chosen_level_in_an_open_session(level):
    request = replace(_walk([0]), level=ac.LevelPolicy(level_db=level, resolved=ac.ResolvedLevel(75, -15, "1234")))
    if level == -25:
        with pytest.raises(ac.LateralWalkRefused) as refused:
            await _run_gated(request)
        assert refused.value.reason == ac.WALK_LEVEL_POLICY_INVALID
    else:
        result, _ = await _run_gated(request)
        assert result.status == "complete"


async def test_bass_levels_refuse_when_no_level_is_admissible():
    request = ac.AngleCaptureRequest(stops=(ac.AngleStop(0, ac.REGIME_SUMMED, purpose="bass"),))
    ladder = level_ladder(request, ready_facts(request, commissioning_stop_db_spl=None))
    hold, prepare = Mock(), Mock()
    with pytest.raises(ac.LateralWalkRefused) as refused:
        await run_levels(ladder, hold=hold, prepare=prepare, gate=AnsweredGate(), aborts=_ABORTS)
    assert refused.value.reason == "walk_commissioning_stop_unset"
    assert not hold.mock_calls and not prepare.mock_calls


@pytest.fixture
def rung_spl(monkeypatch):
    measurements = {}
    bank = _Store.bank

    async def measured_bank(self, record):
        if "level_db" in record:
            level = record["level_db"]
            record["capture_integrity"] = {"spl": measurements.get(round(level, 2), {
                "loudest_half_second_db_spl": 93 + level, "max_window_db_spl": 93 + level,
                "ceiling_db_spl": 85})}
            record["program_id"] = "bass-sweep"
        return await bank(self, record)

    monkeypatch.setattr(_Store, "bank", measured_bank)
    return measurements


@pytest.mark.parametrize("partial", [False, True, "last", "all", "stop"])
async def test_bass_levels_keep_one_hold_and_finish_each_pose(tmp_path, box, partial, rung_spl):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    request = _walk([0, 20], candidates=("base",))
    request = replace(request, stops=tuple(replace(stop, purpose="bass") for stop in request.stops))
    facts = ready_facts(request)
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record,
        "ambient_report": {"bands": [{"band_hz": [20, 80], "level_dbfs": -60}]}}))
    ladder = preflight_levels(request, facts, "-28,-23,-18")
    fakes, gate, manifests = FakeSeams(), AnsweredGate(), []
    packet = RoundPacket(RunManifest("ladder", _Store(fakes.records)), ladder.to_dict())
    entry_volume = box.volume_db

    def prepare(plan):
        assert fakes.graph.restores == 0
        manifest = RunManifest(f"run-{len(manifests)}", packet)
        manifests.append(manifest)
        door = _run_door(tmp_path, box, fakes, manifest)
        verdict = (TakeVerdict(False, fault=REASON_SPL_CEILING_EXCEEDED, next="stop") if partial == "stop" else
                   TakeVerdict(False, fault=REASON_CLIPPED, next="fix_and_retake")
                   if partial and (partial == "all" or len(manifests) == (6 if partial == "last" else 1)) else TakeVerdict(True))
        return LevelRun(manifest, door, _analysis, lambda *_args, **_kwargs: verdict)

    hold = _run_door(tmp_path, box, fakes, RunManifest("unused", _Store(fakes.records))).hold
    signals = plan_run.RunSignals()
    results = await run_levels(ladder, hold=hold, prepare=prepare, gate=gate, aborts=_ABORTS, signals=signals)
    await packet.finish()
    document = packet.to_dict()
    expected = ([(0, -28)] if partial == "stop" else
                [(pose, level) for pose in (0, 20) for level in (-28, -23, -18)])
    assert [(call["position_deg"], call["level_db"]) for call in fakes.play.calls] == expected
    assert len(gate.grants) == (1 if partial == "stop" else 2)
    assert sum(result.mic_moves for result in results) == len(gate.grants)
    assert all(result.finalized for result in results)
    statuses = ["partial" if partial and (partial == "all" or index == (5 if partial == "last" else 0)) else "complete"
                for index in range(len(expected))]
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
    assert len({result.run_id for result in results}) == len(expected)
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
    request = replace(request, stops=(replace(request.stops[0], purpose=purpose),))
    result, _ = await _run_gated(request, analyze=lambda *_args: analysis)
    assert result.status == "partial"
    take = _takes(result.to_dict())[0]
    assert take["screens"][0]["blocking"] is True
    assert take["screens"][0]["evidence"]["pilot_snr_ok"] is False
    assert take["screens"][0]["evidence"]["pilots"][0]["level_hi_dbfs"] == -65

    manifest = RunManifest("pilot", _Store(FakeSeams().records), program=purpose)
    manifest.begin({"index": 1, "repeat": 1, "pose": {"kind": "bearing", "deg": 0}}, attempt=1, pose_index=0)
    await manifest.append({"take_id": "pilot", "program": program.to_dict()}, "record", verdict,
                          complete=True, started_s=0, ended_s=1, level_observation={})
    root = await asyncio.to_thread(bank_seat_round, tmp_path / "round")
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


@pytest.mark.parametrize("stop,tolerance,fader,admitted,window", [
    (85, 1, -19.99, 76.47, 82), (90.53, 1, -14.46, 82, 87.53),
    (85, 0.5, -19.99, 76.47, 82), (87, 4, -18.99, 77.47, 83),
])
async def test_ladder_caps_from_previous_measured_window(tmp_path, box, rung_spl, stop, tolerance, fader, admitted, window):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    request = ac.AngleCaptureRequest((ac.AngleStop(0, ac.REGIME_SUMMED, purpose="bass"),))
    facts = ready_facts(request, commissioning_stop_db_spl=stop, program_ids_for=lambda _: ("bass-sweep",))
    anchor_stimulus = {"program_id": "broadband-sweep", "wav_sha256": "anchor-wav"}
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record, "reference_volume_db": -22.23, "measured_db_spl": 74.23,
        "target": {"target_db_spl": 75, "tolerance_db": tolerance},
        "stimulus": anchor_stimulus}))
    rung_spl.update({level: {"loudest_half_second_db_spl": half, "max_window_db_spl": peak,
                            "ceiling_db_spl": stop}
                    for level, half, peak in ((-31.46, 66.22, 71.11), (-21.46, 76.04, 80.53))})
    ladder = preflight_levels(request, facts, "-14.46,-31.46,-21.46")
    fakes, gate = FakeSeams(), AnsweredGate()
    packet = RoundPacket(RunManifest("ladder", _Store(fakes.records)), json.loads(json.dumps(ladder.to_dict())))

    def prepare(plan):
        manifest = RunManifest(f"run-{len(packet.runs)}", packet)
        return LevelRun(manifest, _run_door(tmp_path, box, fakes, manifest), _analysis,
                        lambda *_args, **_kwargs: TakeVerdict(True))

    hold = _run_door(tmp_path, box, fakes, RunManifest("unused", _Store(fakes.records))).hold
    results = await run_levels(ladder, hold=hold, prepare=prepare, gate=gate, aborts=_ABORTS,
                               save_ladder=packet.update_schedule)
    await packet.finish()
    assert [call["level_db"] for call in fakes.play.calls] == pytest.approx([-31.46, -21.46, fader])
    assert all(result.status == "complete" for result in results)
    schedule = packet.manifest.records.snapshots[-1]["schedule"]
    assert schedule["anchor_stimulus"] == anchor_stimulus
    first, second, third = schedule["admissions"]
    assert [row["requested_db_spl"] for row in (first, second, third)] == [65, 75, 82]
    assert first["basis"] == "unmeasured_stimulus_opener"
    observation, = first["observations"]
    assert observation["run_stimulus"]["program_id"] == "bass-sweep"
    assert observation["stimulus_mismatch"] is True
    assert observation["measured_offset_db"] == pytest.approx(1.22)
    assert second["basis"] == third["basis"] == "measured_window"
    assert third["admitted_db_spl"] == pytest.approx(admitted)
    assert third["level_db"] == pytest.approx(fader)
    assert third["previous_level_db"] == pytest.approx(-21.46)
    assert third["max_window_db_spl"] == 80.53
    assert third["predicted_max_window_db_spl"] == pytest.approx(window)
    assert third["margin_db"] == max(tolerance, 3)
    assert third["bound_by"] == ("measured_window_crest" if admitted < 82 else None)


async def test_opener_cap_survives_plan_serialization_at_each_pose(tmp_path, box, rung_spl):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    request = ac.AngleCaptureRequest(tuple(ac.AngleStop(angle, ac.REGIME_SUMMED, purpose="bass") for angle in (0, 20)))
    facts = ready_facts(request, program_ids_for=lambda _: ("bass-sweep",))
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record, "reference_volume_db": -22.23, "measured_db_spl": 74.23}))
    rung_spl[-22.23] = {"loudest_half_second_db_spl": 75, "max_window_db_spl": 80, "ceiling_db_spl": 85}
    preview = preflight_levels(request, facts, "-12.46,-11.46")
    received = ac.AngleCaptureRequest.from_mapping(json.loads(json.dumps(preview.plan.to_dict())))
    ladder = preflight_levels(received, facts)
    fakes, gate = FakeSeams(), AnsweredGate()
    packet = RoundPacket(RunManifest("ladder", _Store(fakes.records)), json.loads(json.dumps(ladder.to_dict())))

    def prepare(plan):
        manifest = RunManifest(f"run-{len(packet.runs)}", packet)
        return LevelRun(manifest, _run_door(tmp_path, box, fakes, manifest), _analysis,
                        lambda *_args, **_kwargs: TakeVerdict(True))

    hold = _run_door(tmp_path, box, fakes, RunManifest("unused", _Store(fakes.records))).hold
    await run_levels(ladder, hold=hold, prepare=prepare, gate=gate, aborts=_ABORTS, save_ladder=packet.update_schedule)
    assert [call["level_db"] for call in fakes.play.calls] == pytest.approx([-22.23, -20.23] * 2)
    admissions = packet.manifest.records.snapshots[-1]["schedule"]["admissions"]
    assert [row["pose_index"] for row in admissions] == [1, 1, 2, 2]
    for opener in admissions[::2]:
        assert opener["requested_db_spl"] == 84
        assert opener["admitted_db_spl"] == pytest.approx(74.23)
        assert opener["bound_by"] == "unmeasured_stimulus_opener"


@pytest.mark.parametrize("window", [None, float("nan"), float("inf"), float("-inf"), True, "80.53", "empty"])
async def test_unmeasured_rung_holds_or_persists_its_missing_level(tmp_path, box, rung_spl, monkeypatch, window):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    request = ac.AngleCaptureRequest((ac.AngleStop(0, ac.REGIME_SUMMED, purpose="bass"),))
    ladder = preflight_levels(request, ready_facts(request), "-28,-18")
    rung_spl[-28] = {"loudest_half_second_db_spl": 66.22, "max_window_db_spl": window, "ceiling_db_spl": 85}
    fakes, gate = FakeSeams(), AnsweredGate()
    packet = RoundPacket(RunManifest("ladder", _Store(fakes.records)), json.loads(json.dumps(ladder.to_dict())))
    if window == "empty":
        async def without_records(_request, *, manifest, **_kwargs):
            return manifest
        monkeypatch.setattr("jasper.active_speaker.run_levels.run_plan", without_records)

    def prepare(plan):
        manifest = RunManifest(f"run-{len(packet.runs)}", packet)
        return LevelRun(manifest, _run_door(tmp_path, box, fakes, manifest), _analysis,
                        lambda *_args, **_kwargs: TakeVerdict(True))

    hold = _run_door(tmp_path, box, fakes, RunManifest("unused", _Store(fakes.records))).hold
    run = run_levels(ladder, hold=hold, prepare=prepare, gate=gate, aborts=_ABORTS, save_ladder=packet.update_schedule)
    if window == "empty":
        with pytest.raises(ac.LateralWalkRefused) as refused:
            await run
        assert refused.value.reason == "walk_level_policy_invalid"
    else:
        assert all(result.status == "complete" for result in await run)
    assert [call["level_db"] for call in fakes.play.calls] == ([] if window == "empty" else [-28, -28])
    blocked = packet.manifest.records.snapshots[-1]["schedule"]["admissions"][-1]
    assert (blocked["pose_index"], blocked["level_index"], blocked["requested_db_spl"]) == (1, 2, 75)
    if window == "empty":
        assert blocked["status"] == "blocked" and blocked["admitted_db_spl"] is None
    else:
        assert blocked["bound_by"] == "previous_rung_unmeasured" and blocked["level_db"] == -28
        assert blocked["admitted_db_spl"] == 65
    assert blocked["unavailable"] == (["previous_rung"] if window == "empty" else ["max_window_db_spl"])


async def test_room_plan_levels_keep_pose_order(tmp_path, box, tuning_profile, rung_spl):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    candidate = _room_candidate(tuning_profile)
    program = run_program("room")
    levels = (-10.0, -20.0)
    rung_spl[-20] = {"loudest_half_second_db_spl": 73, "max_window_db_spl": 73, "ceiling_db_spl": 95}
    request = ac.request_for_program(program, candidates=("base", candidate.fingerprint), levels=levels)
    report = preflight_levels(request, ready_facts(request, candidates={candidate.fingerprint: candidate}, commissioning_stop_db_spl=95))
    assert not report.blocking
    fakes, gate, manifests = FakeSeams(), AnsweredGate(), []

    def prepare(plan):
        manifest = RunManifest(f"run-{len(manifests)}", _Store(fakes.records))
        manifests.append(manifest)
        return LevelRun(manifest, _run_door(tmp_path, box, fakes, manifest), _analysis,
                        lambda *_args, **_kwargs: TakeVerdict(True), prepare_level_captures(plan))

    assert sum(len(row.schedule) for row in report.levels) == 3 * 2 * len(levels)
    hold = _run_door(tmp_path, box, fakes, RunManifest("unused", _Store(fakes.records))).hold
    results = await run_levels(report, hold=hold, prepare=prepare, gate=gate, aborts=_ABORTS)
    expected = [(pose.seat_offset_m, level, cid) for pose in program.poses
                for level in sorted(levels) for cid in ("banked-base", candidate.fingerprint)]
    assert [(row["seat_offset_m"], row["level_db"], row["candidate_id"]) for row in fakes.records.banked] == expected
    assert len(fakes.play.calls) == len(expected)
    assert len(gate.grants) == 3 and fakes.graph.restores == 1
    assert all(result.status == "complete" for result in results)


async def test_run_door_preemption_defers_volume_restore_and_restores_graph(tmp_path, box):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    fakes = FakeSeams()
    manifest = RunManifest("run", _Store(fakes.records))
    door = _run_door(tmp_path, box, fakes, manifest)
    request = replace(_walk([0, 20, 40]), level=ac.LevelPolicy(resolved=ac.ResolvedLevel(75, -20, "1234")))
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
            aborts=_ABORTS, measure=measure,
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
    manifest = RunManifest("run", _Store(FakeSeams().records), level={"session": {"session_id": "leveled"}})
    cases = [(0, -15, "a", "", 70, True, None), (0, -15, "a", "", 72, True, 2), (0, -15, "a", "", 90, False, 19),
             (20, -15, "a", "", 76, True, 5), (0, -15, "a", "", 72, True, 1), (0, -25, "a", "", 60, True, None),
             (0, -15, "b", "", 55, True, None), (0, -15, "b", "", 58, True, 3), (0, -15, "a", "", 73, True, 1),
             (0, -15, "a", "fp-cut", 64, True, None), (0, -15, "a", "fp-cut", 65, True, 1)]
    for index, (pose, gain, program, candidate, observed, accepted, delta) in enumerate(cases):
        manifest.begin({"index": index, "pose": {"kind": "bearing", "deg": pose}, "candidate_id": candidate},
                       attempt=1, pose_index=index)
        record = {"take_id": str(index), "level_db": -99, "provenance": {"session_volume_db": gain}, "phase": "measure", "program_id": program,
                  "capture_integrity": {"spl": {"loudest_half_second_db_spl": observed, "max_window_db_spl": 99}}}
        await manifest.append(record, str(index), TakeVerdict(accepted), complete=True, started_s=0, ended_s=1,
                              level_observation=plan_run.level_drift_verdict(**manifest.level_observation(record)).evidence)
        row = next(take for take in manifest.takes if take["take_id"] == str(index))
        assert row["level"]["loudest_half_second_db_spl"] == observed
        assert row["level"]["level_delta_db"] == delta
        assert row["phase"] == "measure"
    assert manifest.to_dict()["level"]["session"]["session_id"] == "leveled"


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


def test_a_driver_pose_is_timed_as_its_probe_and_its_takes():
    """A driver's pose plays its whole level probe once before its takes; a
    far-field pose plays no probe (ADR-0365)."""
    band = RoleBand("woofer", 0, FrequencyBand(20, 2000))
    take = build_measure_program({"woofer": -20.0}, (band,), repeat_count=1, sweep_durations={"woofer": 0.2})
    probe = build_level_probe_program(band, (-40.0, -34.0), sweep_band_hz=(20.0, 2000.0), gap_s=0.5,
                                      downstream_gain_db=0.0, channels=1)
    driver = SimpleNamespace(graph_scope="drivers", candidate_id="", program_phase="lateral")
    far = SimpleNamespace(graph_scope="drivers", candidate_id="", program_phase="lateral")
    captures = [({"place": "at_driver", "driver": "woofer"}, driver)] * 2 + [({"place": "far"}, far)]

    facts = plan_run.schedule_facts(
        captures, lambda spec, stimulus_dbfs=None: probe if spec is driver and stimulus_dbfs is None else take,
        mover="arm")

    take_s = sum(segment.n_samples for segment in take.stimulus_segments()) / take.sample_rate_hz
    assert facts["estimated_seconds"] == pytest.approx(3 * take_s + probe.total_samples / probe.sample_rate_hz)


@pytest.mark.parametrize("repeats, counts, timing, preparation", [(1, [15, 8, 8], 1, 12), (2, [26, 16, 16], 2, 20)])
def test_three_pose_preview_counts_preparation_and_timing(repeats, counts, timing, preparation):
    context = SimpleNamespace(roles_bands=tuple(_roles()), driver_caps_dbfs={}, fc_hz=2500,
                              driver_sweep_duration_limits_s={}, driver_bands={}, safety_profile={}, role_targets={})
    request = ac.request_for_program(measurement_program("tournament", "full"), repeats=repeats)
    captures = plan_run.prepare_plan_captures(request, roles_bands=context.roles_bands)
    facts = plan_run.preview_schedule(request, captures, context)
    assert facts["measurements"] == len(captures)
    assert sum(facts["measurements_per_pose"]) == len(captures)
    assert facts["sweeps_per_pose"] == counts
    assert facts["timing_sweeps"] == timing
    assert facts["preparation_sweeps"] == preparation
    assert facts["sweeps"] == sum(counts)
    timing_rows = [row for row in facts["pose_sweeps"][0] if row["kind"] == "summed_sweep"]
    assert [(row["repeat"], row["repeats"]) for row in timing_rows] == [(n, repeats) for n in range(1, repeats + 1)]


async def test_a_ladder_ends_on_the_counts_its_banked_manifest_prints(monkeypatch, box):
    """The ladder's last published facts count from the joined manifest that
    ``wait`` reprints once banked, so the two "Measured" lines agree."""
    joined = {"status": "complete", "reason": "", "level": {}, "runs": [], "honoured": {},
              "sets": [{"takes": [{"take_id": "t1", "selected": True}, {"take_id": "t2", "selected": False}]}],
              "not_measured": [{"pose": {"deg": 0}, "reason": "summed_sweep_heard"}] * 3}
    packet = SimpleNamespace(runs={}, to_dict=lambda: joined, finish=AsyncMock(), update_schedule=AsyncMock())
    monkeypatch.setattr(correction_run_host, "RoundPacket", lambda *_args: packet)
    monkeypatch.setattr(correction_run_host, "bind_plan_analysis", lambda *a, **kw: (None, None))
    monkeypatch.setattr(correction_run_host, "resolved_household_sensitivity", lambda _: None)
    monkeypatch.setattr(correction_run_host, "run_levels", AsyncMock(return_value=[]))
    gate = AnsweredGate()
    _, _, _, execute = correction_run_host.bind_run_door(
        host=None, device=None, evidence_store=None, manifest=RunManifest("packet", _Store(FakeSeams().records)),
        production=FakeSeams(), conductor=None, refs={}, trims={}, ceiling_s=30, ceiling_db_spl=85,
        camilla_factory=lambda: box,
        ladder=SimpleNamespace(admissible=[None], plan=SimpleNamespace(levels=(-23,)), to_dict=lambda: {}),
    )
    await execute(None, gate=gate, signals=plan_run.RunSignals(), captures=())
    ended = gate.progress[-1]
    assert (ended["takes"], ended["not_measured"]) == (1, 3)
    assert round_lines(ended)[0] == coverage_lines({}, joined)[0]


@pytest.mark.parametrize("site", ["transaction", "executor", "ladder"])
async def test_run_host_banks_admission_failure_code_and_segments(monkeypatch, tmp_path, box, site):
    from tests.test_correction_crossover_v2_wired import _run_door  # lazy: fixture module imports this module

    admission = ProgramAdmission("verify", "verify", -23, (
        SegmentAdmission("summed-1", "summed", 0, (20, 20000), -23, False, ()),
    ), (), (ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS,))
    failure = ProgramPlaybackRefused(admission)
    fakes = FakeSeams()
    store = _Store(fakes.records)
    outer = RunManifest("packet", store)
    gate = AnsweredGate()
    if site == "ladder":
        monkeypatch.setattr(correction_run_host, "bind_plan_analysis", lambda *a, **kw: (None, None))
        monkeypatch.setattr(correction_run_host, "resolved_household_sensitivity", lambda _: None)
        monkeypatch.setattr(correction_run_host, "run_levels", AsyncMock(side_effect=failure))
        _, _, _, execute = correction_run_host.bind_run_door(
            host=None, device=None, evidence_store=None, manifest=outer, production=fakes,
            conductor=None, refs={}, trims={}, ceiling_s=30, ceiling_db_spl=85,
            camilla_factory=lambda: box,
            ladder=SimpleNamespace(admissible=[None], plan=SimpleNamespace(levels=(-23,)), to_dict=lambda: {}),
        )
        with pytest.raises(ProgramPlaybackRefused):
            await execute(None, gate=gate, signals=plan_run.RunSignals(), captures=())
    else:
        packet = RoundPacket(outer, {})
        manifest = RunManifest("run", packet)
        if site == "transaction":
            play = AsyncMock()
            prepared = ProgramForStimulus(SimpleNamespace(program_id="verify", phase="verify"), {
                "readmit": AsyncMock(return_value=admission), "play_wav": play, "writer_lock": Mock(),
            })
            monkeypatch.setattr(fakes.play, "run", ProgramPlaybackTransaction(
                compose=lambda **kw: prepared, session_volume_plan=SimpleNamespace(assert_ready=Mock()),
            ).run)
        else:
            monkeypatch.setattr(fakes.play, "run", AsyncMock(side_effect=failure))
        door = _run_door(tmp_path, box, fakes, manifest)
        request = replace(_walk([0, 20]), level=ac.LevelPolicy(resolved=ac.ResolvedLevel(75, -20, "1234")))
        run = plan_run.run_plan(request, door=door, manifest=manifest, analyze=_analysis, gate=gate, aborts=_ABORTS)
        if site == "executor":
            with pytest.raises(ProgramPlaybackRefused):
                await run
        else:
            await run
            play.assert_not_awaited()
        await packet.finish()
        assert packet.runs["run"]["reason"] == "program_admission_refused"
        assert all(row["reason"] == "program_admission_refused" for row in manifest.not_measured)
    saved = store.snapshots[-1]
    assert saved["reason"] == "program_admission_refused"
    if site == "transaction":
        assert saved["sets"][0]["takes"][0]["quality"]["evidence"]["admission"] == admission.to_dict()
