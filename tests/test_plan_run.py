# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The executor's observable work, placement, evidence and control contract."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from unittest.mock import Mock

import pytest

from jasper.active_speaker import angle_capture as ac, plan_run
from jasper.active_speaker.crossover_v2.admission import MAX_AUTOMATIC_RETAKES_PER_POSITION
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_CANDIDATE, POSITION_AXIS_VERTICAL
from jasper.active_speaker.crossover_v2.position_gate import POSITION_HOLD_EXPIRED_CODE, PositionGate
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY, REASON_DRIFT_BASELINES_DISAGREE, REASON_CLIPPED, REASON_ANCHOR_AMBIGUOUS,
    TakeVerdict,
)
from jasper.active_speaker.run_manifest import RunManifest, RUN_MANIFEST_KIND, TAKE_INCOMPLETE
from jasper.audio_measurement.program_analysis import ProgramAnalysis
from tests.crossover_v2_fixtures import _loc
from tests.engine_twin import FakeGraph, FakeSeams, FakePlay, SeamFailure, open_session

_ABORTS = {SeamFailure: "seam_failed"}
_SCOPES = {"fp-a": "candidate", "fp-b": "candidate"}


def _walk(angles, candidates=("fp-a",)):
    return ac.AngleCaptureRequest(candidates=candidates, stops=tuple(
        ac.AngleStop(angle, ac.REGIME_SUMMED, candidate_id=candidate)
        for angle in angles for candidate in candidates),
        template=ac.walk_template(kind=MEASURE_KIND_CANDIDATE))


def _analysis(_record, _record_id):
    return ProgramAnalysis(phase="verify", program_id="test", locations=(_loc("sweep"),))


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
        self.progress = []

    def publish(self, progress):
        super().publish(progress)
        self.progress.append(self.published()["run"])

    def gate(self, index, attempt, entry):
        try:
            super().gate(index, attempt, entry)
        except CaptureBeginDeferred:
            pending = self.published()["pending"]
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


async def _run_gated(request, *, seams=None, gate=None, analyze=_analysis, signals=None):
    fakes = seams or FakeSeams()
    manifest = RunManifest("run", _Store(fakes.records))
    async with open_session(replace(fakes, records=manifest)) as (session, _):
        result = await plan_run.run_plan(request, session=session, manifest=manifest, analyze=analyze,
                                         gate=gate, candidate_scopes=_SCOPES, aborts=_ABORTS, signals=signals)
    return result, fakes


def _takes(document):
    return [take for group in document["sets"] for take in group["takes"]]


@pytest.mark.parametrize(("angles", "candidates"), [([0], ("fp-a",)), ([0, 20], ("fp-a", "fp-b")), ([0, -20, 20], ("fp-a",))])
@pytest.mark.parametrize("repeats", [1, 3])
def test_a_walk_groups_configs_and_repeats_under_one_pose_grant(angles, candidates, repeats):
    request, gate = replace(_walk(angles, candidates), repeats=repeats), AnsweredGate()
    result, fakes = asyncio.run(_run_gated(request, gate=gate))
    assert result.status == "complete"
    assert len(result.wall_s) == result.mic_moves == len(gate.grants) == len(angles)
    assert result.takes_measured == len(fakes.banked) == len(angles) * len(candidates) * repeats
    assert fakes.graph.scopes == [("candidate", cid) for _ in angles for cid in candidates for _ in range(repeats)]
    assert fakes.graph.restores == fakes.volume.releases == 1
    doc = json.loads(json.dumps(result.to_dict()))
    assert len(doc["sets"]) == len(set(candidates))
    for group in doc["sets"]:
        assert {take["pose"]["deg"] for take in group["takes"]} == set(angles)
        assert {take["repeat"] for take in group["takes"]} == set(range(1, repeats + 1))
    assert doc["not_measured"] == []
    assert doc["honoured"]["takes_refused"] == 0
    assert all(row["budget"]["by_household"] == row["budget"]["by_speaker"] == 0 for row in gate.progress)


def test_skipped_per_driver_work_is_disclosed_without_an_extra_grant():
    request = ac.AngleCaptureRequest(candidates=("fp-a", "base", "fp-b"), stops=(
        ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-a"),
        ac.AngleStop(0, ac.REGIME_PER_DRIVER), ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-b")))
    gate = AnsweredGate()
    result, _ = asyncio.run(_run_gated(request, gate=gate))
    assert result.status == "partial"
    assert (result.stops_planned, result.takes_measured, result.takes_skipped) == (3, 2, 1)
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
    assert take["quality"]["fault"] in REASON_REGISTRY
    assert take["next_action"] == "stop"
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
    _walk([0], ("fp-z",)), ac.per_driver_at([0]),
])
def test_unplayable_plan_records_each_missing_stop(plan):
    result, fakes = asyncio.run(_run_gated(plan))
    assert result.status == "partial"
    assert result.reason in {ac.WALK_STIMULUS_NOT_ACCEPTED, ac.WALK_NOTHING_PLAYABLE}
    assert len(result.not_measured) == len(plan.stops)
    assert fakes.play.calls == []


@pytest.mark.parametrize(("next_action", "gain"), [("retake_louder", -15), ("retake_quieter", -24)])
def test_retry_recomposes_at_requested_gain_and_keeps_both_takes(monkeypatch, next_action, gain):
    verdicts = iter([TakeVerdict(False, REASON_CLIPPED, next=next_action, next_gain_db=gain, charge="speaker"), TakeVerdict(True)])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    gate = AnsweredGate()
    result, fakes = asyncio.run(_run_gated(_walk([0]), gate=gate))
    assert fakes.play.rungs == [None, gain]
    assert len(fakes.banked) == 2
    assert gate.grants == [(1, 1)]
    assert [t["attempt"] for t in result.takes] == [1, 2]
    assert [t["selected"] for t in _takes(result.to_dict())] == [False, True]
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
    assert result.reason == REASON_DRIFT_BASELINES_DISAGREE


def test_fix_and_retake_needs_a_fresh_same_pose_grant(monkeypatch):
    verdicts = iter([TakeVerdict(False, REASON_ANCHOR_AMBIGUOUS, next="fix_and_retake", charge="operator"), TakeVerdict(True), TakeVerdict(True)])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    gate = AnsweredGate()
    result, _ = asyncio.run(_run_gated(_walk([0], ("fp-a", "fp-b")), gate=gate))
    assert gate.grants == [(1, 1), (1, 2)]
    assert result.status == "complete"
    assert any(row["fault"] == REASON_ANCHOR_AMBIGUOUS and row["next_action"] == "fix_and_retake" for row in gate.progress)


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
    assert result.takes[0]["quality"]["fault"] == REASON_CLIPPED
    assert result.takes[0]["next_action"] == ("stop" if action == "accept" else action)
    assert [stop["index"] for stop in result.not_measured] == ([] if retried else [1, 2])
    assert all(stop["reason"] == REASON_CLIPPED for stop in result.not_measured)


@pytest.mark.parametrize("failure", [None, SeamFailure, RuntimeError, asyncio.CancelledError])
def test_timing_excludes_placement_and_all_exits_publish_terminal_state(monkeypatch, failure):
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
                raise failure()
            return await super().run(**kwargs)
    gate, fakes = MovingGate(), FakeSeams(play=TimedPlay())
    store = _Store(fakes.records)
    manifest = RunManifest("run", store)
    event = Mock(wraps=plan_run.log_event)
    monkeypatch.setattr(plan_run, "log_event", event)
    async def run():
        async with open_session(replace(fakes, records=manifest)) as (session, _):
            return await plan_run.run_plan(_walk([0]), session=session, manifest=manifest, analyze=_analysis,
                                           gate=gate, candidate_scopes=_SCOPES, aborts=_ABORTS, clock=lambda: now)
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
    assert len(result.not_measured) == (0 if accepted else 1)


@pytest.mark.parametrize("action", ["accept", "retake_same", "stop", "complete", "cancel"])
def test_run_never_applies_a_tune(monkeypatch, action):
    from jasper.active_speaker import baseline_profile
    from jasper.web import correction_crossover_v2
    apply = Mock(side_effect=AssertionError("apply called"))
    monkeypatch.setattr(baseline_profile, "apply_baseline_profile", apply)
    monkeypatch.setattr(correction_crossover_v2, "handle_v2_apply", apply)
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


@pytest.mark.parametrize(
    ("stated", "expected"), [(None, 85.0), (80.0, 80.0), (85.0, 85.0)],
    ids=["none-takes-the-stop", "under-the-stop", "at-the-stop"],
)
def test_a_run_plays_under_the_commissioning_stop_by_default(
    stated: float | None, expected: float,
) -> None:
    """A run stating no ceiling is not asking to be unbounded: the box's own
    commissioning stop is what bounds it."""
    assert plan_run.take_spl_ceiling(
        stated, commissioning_stop_db_spl=85.0,
    ) == expected


def test_a_ceiling_above_the_commissioning_stop_is_refused_not_clamped() -> None:
    """The number was typed; clamping it would let an operator believe a louder
    measurement had been allowed."""
    with pytest.raises(ac.LateralWalkRefused) as refused:
        plan_run.take_spl_ceiling(90.0, commissioning_stop_db_spl=85.0)

    assert refused.value.reason == ac.WALK_CEILING_ABOVE_STOP


@pytest.mark.parametrize(
    ("ceiling", "note"),
    [(None, plan_run.SPL_MONITOR_UNAVAILABLE), (85.0, "ceiling_85_db_spl")],
    ids=["no-calibration", "watched"],
)
def test_the_run_discloses_what_watched_its_level(
    ceiling: float | None, note: str,
) -> None:
    """A box that cannot turn a recording into dB SPL says so rather than
    claiming a bound nothing measured."""
    assert plan_run.spl_monitor_note(ceiling) == note



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
        graph_fingerprint = fakes.graph.fingerprint

        async def measure(self, spec):
            async with open_session(replace(fakes, records=manifest)) as (session, _):
                return await session.measure(spec)

    result = asyncio.run(plan_run.run_plan(_walk([0, 20]), session=FreshEngine(), manifest=manifest,
                         analyze=_analysis, gate=AnsweredGate(), candidate_scopes=_SCOPES, aborts=_ABORTS))
    root = Path(info["bundle_dir"]) / "evidence/v1/artifacts"
    document = json.loads((root / result.path).read_text())
    records = [json.loads((root / take["artifacts"]["record_id"]).read_text()) for take in _takes(document)]
    assert len({record["take_id"] for record in records}) == 2
    assert document["status"] == "complete"
    assert not (root / "crossover_v2/run/round_receipt.json").exists()


def test_retake_while_next_pose_waits_returns_to_previous_observation(monkeypatch):
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
    assert fakes.play.bearings == [0, 0, 20]
    assert [t["selected"] for t in _takes(result.to_dict())] == [False, True, True]
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
    assert result.takes[0]["quality"]["fault"] in REASON_REGISTRY


def test_real_assessor_sees_glitch_and_retries_once():
    calls = 0
    def analyze(record, record_id):
        nonlocal calls
        calls += 1
        return replace(_analysis(record, record_id), discontinuity_samples=1024 if calls == 1 else 0)
    result, fakes = asyncio.run(_run_gated(replace(_walk([0]), retries_per_pose=0), analyze=analyze))
    assert len(fakes.banked) == 2
    assert result.takes[0]["quality"]["fault"] == REASON_DRIFT_BASELINES_DISAGREE
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
            await manifest.append({**record, **(changed if index == 2 else {})}, f"record-{index}",
                                  TakeVerdict(True), complete=True, started_s=index, ended_s=index + 1)
    asyncio.run(append())
    groups = manifest.to_dict()["sets"]
    assert len(groups) == (2 if changed else 1)
    assert {t["pose"]["deg"] for t in groups[0]["takes"]} == ({0, 20} if changed else {0, 10, 20})


def test_manifest_names_emitted_role_levels_and_usable_bands():
    manifest = RunManifest("run", _Store(FakeSeams().records))
    manifest.begin({"index": 1, "repeat": 1, "pose": {"deg": 0}}, attempt=1, pose_index=0)
    record = {"stimulus_dbfs": -12, "program": {"segments": [
        {"kind": "pilot", "role": "pilot_only", "gain_db": -6},
        {"kind": "sweep", "role": "woofer", "gain_db": -18},
        {"kind": "sweep", "role": "tweeter", "gain_db": -24},
    ]}, "curves": [{"role": "woofer", "band_hz": [20, 2000], "validity_floor_hz": 100}]}
    asyncio.run(manifest.append(record, "record", TakeVerdict(True), complete=True, started_s=0, ended_s=1))
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
