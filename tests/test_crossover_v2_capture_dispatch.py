# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import ast
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jasper.active_speaker.crossover_v2 import capture_dispatch as cd, refusal_copy
from jasper.active_speaker.alignment_evidence import round_alignment
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.measurement_programs import SEAT_LEVEL, SPOT_LEVEL
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.run_manifest import RunManifest
from jasper.audio_measurement.wired_capture import WIRED_POST_ROLL_S, WiredCaptureAnswer
from jasper.web.correction_run_host import bind_plan_analysis, compose_plan_program
from jasper.audio_measurement import snr_policy
from jasper.audio_measurement.frame_ledger import FrameLedger
from jasper.audio_measurement.level import LevelReading
from jasper.audio_measurement.admission.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    KIND_SWEEP, RoleBand, build_level_probe_program, build_measure_program,
)
from jasper.audio_measurement.program_analysis.model import (
    SWEEP_LOCATE_CONFIDENCE_FLOOR, SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    AnchorEvidence, DriftEstimate, GainPlan, MeasurementPriors, ProgramAnalysis,
)
from jasper.audio_measurement.quality_model import DRIVER
from jasper.platform import control_client
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _alignment,
    _conductor,
    _driver_response,
    _loc,
    _measure_analysis,
    _snr_pilot,
)
from tests.test_plan_run import AnsweredGate, _run_gated, _walk
from tests._log_events import event_field_maps

PHASES = ("check", "measure", "verify")
GAINS = {"woofer": -30.0, "tweeter": -30.0}


@pytest.fixture(autouse=True)
def output_volume_unknown(monkeypatch):
    read = Mock(return_value={})
    monkeypatch.setattr(cd, "read_output_volume", read)
    return read


def _analysis(**changes):
    return replace(ProgramAnalysis(
        phase="measure", stimulus_id="take", locations=(_loc("sweep_w"),),
        pilot_snr_ok=True, linearity_ok=True, channel_map_ok=True,
        anchor=AnchorEvidence(presence=0.5, confidence=0.9, corroborated=True),
        mic_meter_status="usable", alignment=_alignment(),
        driver_responses=(_driver_response("woofer", 8.0),),
        gain_plan=GainPlan(gain_db=GAINS, predicted_peak_dbfs=-30.0, snr_floor_ok=True),
    ), **changes)


@pytest.mark.parametrize("muted", [True, False, None])
async def test_not_heard_take_stops_only_when_output_is_muted(monkeypatch, caplog, muted):
    response = control_client.ControlResponse(200, b'{"muted": true, "percent": 0}' if muted else b'{"muted": false, "percent": 35}')
    read = Mock(return_value=response, side_effect=control_client.ControlError() if muted is None else None)
    monkeypatch.setattr(control_client, "get_volume", read)
    monkeypatch.setattr(cd, "read_output_volume", control_client.read_output_volume)
    analyses = iter((_analysis(locations=(), pilot_snr_ok=False), _analysis()))
    gate = AnsweredGate()
    caplog.set_level(logging.INFO)
    result, fakes = await _run_gated(_walk([0]), gate=gate, analyze=lambda *_: next(analyses))
    read.assert_called_once_with()
    assert event_field_maps(caplog, "active_speaker.measurement_output_muted") == (
        [{"muted": "true", "household_percent": "0"}] if muted else [])
    assert len(fakes.play.stimulus_dbfs) == (1 if muted else 2)
    assert result.reason == ("measurement_output_muted" if muted else "")
    fault = next(row for row in gate.progress if row.get("fault"))
    assert (fault["fault"], fault["next_action"]) == (
        ("measurement_output_muted", "stop") if muted else ("locate_failed", "fix_and_retake"))
    if muted:
        assert len(gate.grants) == 1
        assert all(take["verdict"]["screens"] == [] for group in result.joined()["sets"] for take in group["takes"])


@pytest.mark.parametrize("muted", [True, False, None])
def test_check_run_host_reads_mute_once(monkeypatch, muted):
    read = Mock(return_value={} if muted is None else {"muted": muted})
    monkeypatch.setattr(cd, "read_output_volume", read)
    conductor = _conductor(FakeSeams(check=lambda _: _analysis(locations=(), pilot_snr_ok=False, linearity_ok=False)),
                           index_phase_map={1: "check"})
    manifest = RunManifest("check", SimpleNamespace(bank=AsyncMock(return_value="manifest")))
    manifest.begin({"index": 1, "candidate_id": "base", "purpose": "speaker", "purposes": ["speaker"], "pose": {"kind": "bearing", "azimuth_deg": 0, "elevation_deg": 0}},
                   attempt=1, pose_index=0)
    records = SimpleNamespace(enrich=None, after_bank=None)
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest, evidence={})
    spec = MeasureSpec(kind="baseline", graph_scope="drivers", program_phase="check")
    program = compose_plan_program(conductor, spec, None)
    record = {"take_id": "take-1", "index": 1, "attempt": 1, "phase": "check", "program": program.to_dict()}
    records.enrich(WiredCaptureAnswer(wav=b"", program=program.to_dict()), record)
    verdict = assessor(analyze(record), phase="check", program=program)
    read.assert_called_once_with()
    assert (verdict.fault, verdict.next) == (
        ("measurement_output_muted", "stop") if muted else ("locate_failed", "fix_and_retake"))
    if muted:
        assert verdict.screens == []
    else:
        assert [screen["code"] for screen in verdict.screens] == ["pilot_level_collapse"]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(("changes", "code", "next", "charge"), [
    ({"locations": ()}, refusal_copy.REASON_LOCATE_FAILED, "fix_and_retake", "operator"),
    ({"locations": (replace(_loc("sweep_w"), role="woofer"),
                    replace(_loc("sweep_t", confidence=cd.LOCATE_MIN_CONFIDENCE / 2), role="tweeter"))},
     refusal_copy.REASON_LOCATE_FAILED, "fix_and_retake", "operator"),
    ({"anchor_ambiguous": True}, refusal_copy.REASON_ANCHOR_AMBIGUOUS, "fix_and_retake", "operator"),
    ({"delta_implausible": True, "pilot_snr_ok": True}, refusal_copy.REASON_PILOT_STEP_IMPLAUSIBLE, "fix_and_retake", "operator"),
    ({"delta_implausible": True, "pilot_snr_ok": False}, refusal_copy.REASON_SNR_FLOOR, "fix_and_retake", "operator"),
    ({"anchor": AnchorEvidence(corroborated=False)}, refusal_copy.REASON_ANCHOR_TOO_QUIET, "fix_and_retake", "speaker"),
    ({"locations": (_loc("sweep_w", clipped=True),)}, refusal_copy.REASON_CLIPPED, "retake_quieter", "speaker"),
    ({"glitch_detected": True}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"frame_ledger": FrameLedger(received_frames=128, declared_frames=256)}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"frame_ledger": FrameLedger(received_frames=128, capture_gaps=1, capture_gap_frames=48)}, refusal_copy.REASON_CAPTURE_OVERRUN, "retake_same", "speaker"),
    ({"discontinuity_samples": -1066.7}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"locations": (_loc("sweep_w", residual_samples=1200.0),)}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"linearity_ok": False}, refusal_copy.REASON_AGC_BEHAVIORAL_FAIL, "fix_and_retake", "operator"),
])
def test_integrity_verdict(phase, changes, code, next, charge):
    verdict = cd.assess(_analysis(**changes), phase=phase, gain_db=GAINS)
    assert not verdict.ok and verdict.fault == code
    assert code in refusal_copy.REASON_REGISTRY
    assert (verdict.next, verdict.charge) == (next, charge)
    assert all(type(value) in (float, bool, str) for value in verdict.evidence.values())
    assert not any(verdict.capabilities.values())
    if next == "retake_quieter":
        assert verdict.next_gain_db == -33.0


#: One ``rear/pair`` take as jts3 round fd97a756ea51 take_0003 measured it, in
#: program order: segment, branch, locate confidence, residual in ms at 48 kHz.
#: The leading pilot pair sits on the front branch, so the rear is unanchored.
TAKE_0003_SWEEPS = (
    ("sweep_w", "front", 0.6982, 0.58),
    ("sweep_t", "rear", 0.2376, 4.13),
    ("sweep_w_rep", "front", 0.6954, 0.85),
    ("sweep_t_rep", "rear", 0.2403, 5.104),
)


def _pair_take(*, rear_role="woofer:rear", branches=True):
    return _analysis(
        locations=tuple(replace(_loc(segment, confidence=confidence,
                                     residual_samples=residual_ms * cd.REQUIRED_SAMPLE_RATE_HZ / 1000),
                                role="woofer" if branch == "front" else rear_role)
                        for segment, branch, confidence, residual_ms in TAKE_0003_SWEEPS),
        pilots=(_snr_pilot("woofer", 30.0),),
        branch_diagnostic={"responses": [{"role": rear_role}]} if branches else None,
    )


@pytest.mark.parametrize("rear_role", ["woofer:rear", None])
@pytest.mark.parametrize("anchor", [None, AnchorEvidence(), AnchorEvidence(
    presence=.5, confidence=.9, runner_up_presence=.01, runner_up_confidence=.4, witnesses_tried=2,
    pair_presence=.6, pair_runner_up_presence=.02,
)])
def test_take_carries_anchor_margin_and_each_roles_sweep_confidence(rear_role, anchor):
    analysis = _pair_take(rear_role=rear_role)
    analysis = replace(analysis, anchor=anchor, locations=(*analysis.locations,
        replace(_loc("pilot", kind="pilot", confidence=.1), role="woofer"),
        _loc("sum", kind="summed_sweep", confidence=.12)))
    evidence = cd.assess(analysis, phase="measure", gain_db=GAINS).evidence
    assert {key: value for key, value in evidence.items() if key.startswith("locate_confidence.")} == {
        "locate_confidence.woofer": .6954, f"locate_confidence.{rear_role or 'summed'}": .2376,
    }
    assert evidence["locate_confidence_min"] == .1
    anchor_figures = {key: evidence[key] for key in (
        "anchor_runner_up_presence", "anchor_runner_up_confidence", "anchor_witnesses_tried",
        "anchor_pair_presence", "anchor_pair_runner_up_presence",
    ) if key in evidence}
    assert anchor_figures == ({
        "anchor_runner_up_presence": .01, "anchor_runner_up_confidence": .4, "anchor_witnesses_tried": 2.0,
        "anchor_pair_presence": .6, "anchor_pair_runner_up_presence": .02,
    } if anchor and anchor.witnesses_tried is not None else {})
    assert all(type(value) is float for value in anchor_figures.values())


@pytest.mark.parametrize(("rear_role", "branches", "on_schedule"), [
    ("woofer:rear", True, True),
    ("tweeter", True, True),
    ("woofer", True, False),
    ("tweeter", False, False),
])
def test_only_a_branch_programs_unanchored_branch_is_judged_on_its_own_path(rear_role, branches, on_schedule):
    analysis = _pair_take(rear_role=rear_role, branches=branches)
    assert cd._sweep_schedule_ok(analysis, cd.REQUIRED_SAMPLE_RATE_HZ) is on_schedule


@pytest.mark.parametrize("role", ["woofer", "woofer:rear", "tweeter"])
@pytest.mark.parametrize("branches", [False, True])
@pytest.mark.parametrize("direction", [-1, 1])
def test_sweep_schedule_is_absolute_for_anchored_roles(role, branches, direction):
    residual = direction * (SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * cd.REQUIRED_SAMPLE_RATE_HZ / 1000 + 1)
    locations = tuple(replace(_loc(segment_id, confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR, residual_samples=residual), role=role)
                      for segment_id in ("sweep_w", "sweep_w_rep"))
    analysis = _analysis(
        locations=locations, pilots=(_snr_pilot(role, 30.0),),
        branch_diagnostic={"responses": [{"role": role}]} if branches else None,
    )
    verdict = cd.assess(analysis, phase="measure", gain_db=GAINS)
    assert not verdict.ok and verdict.fault == refusal_copy.REASON_DRIFT_BASELINES_DISAGREE
    assert (verdict.next, verdict.charge) == ("retake_same", "speaker")
    assert verdict.evidence["guard"] == "sweep_schedule"
    assert verdict.evidence["schedule_residual_ms_worst"] == pytest.approx(residual / cd.REQUIRED_SAMPLE_RATE_HZ * 1000)
    assert verdict.evidence["locate_confidence_min"] == SWEEP_LOCATE_CONFIDENCE_FLOOR


@pytest.mark.parametrize(("sweep_confidence", "pilot_confidence", "ceiling", "code", "next", "charge", "target"), [
    (0.70, 0.12, -20.0, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker", None),
    (0.70, 0.70, -20.0, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker", None),
    (0.15, 0.70, -20.0, refusal_copy.REASON_LOCATE_FAILED, "retake_louder", "speaker", -20.0),
    (0.15, 0.70, -30.0, refusal_copy.REASON_LOCATE_FAILED, "fix_and_retake", "operator", None),
])
def test_failed_schedule_routes_by_sweep_confidence_and_available_gain(
    sweep_confidence,
    pilot_confidence,
    ceiling,
    code,
    next,
    charge,
    target,
):
    band = snr_policy.band_snr_verdicts(
        decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -40}],
        noise_bands=[{"band_id": "mid", "level_dbfs": -70}], noise_floor_dbfs_scalar=None,
        relevant_hz=(1000, 4000), model=DRIVER,
    )
    analysis = _analysis(
        locations=(_loc("pilot_woofer_lo", "pilot", confidence=pilot_confidence),
                   *(_loc(segment, confidence=sweep_confidence, residual_samples=-26e-3 * cd.REQUIRED_SAMPLE_RATE_HZ)
                     for segment in ("sweep_w", "sweep_t", "sweep_w_rep"))),
        driver_responses=(replace(_driver_response("woofer", 8.0), snr={"alignment": band}),),
    )
    verdict = cd.assess(analysis, phase="measure", gain_db=GAINS, gain_ceiling_db={"woofer": ceiling})
    assert not verdict.ok and verdict.fault == code
    assert (verdict.next, verdict.charge, verdict.next_gain_db) == (next, charge, target)
    assert {
        key.removeprefix("next_gain_db."): float(value)
        for key, value in verdict.evidence.items()
        if key.startswith("next_gain_db.")
    } == ({} if target is None else {"woofer": target})
    assert verdict.evidence["locate_confidence_min"] == min(sweep_confidence, pilot_confidence)
    assert verdict.evidence["schedule_residual_ms_worst"] == pytest.approx(-26.0)
    if code == refusal_copy.REASON_DRIFT_BASELINES_DISAGREE:
        assert verdict.evidence["guard"] == "sweep_schedule"
    else:
        assert verdict.evidence["alignment.woofer.alignment_level_db"] == -30.0
        assert verdict.evidence["alignment.woofer.alignment_snr_shortfall_db"] == 5.0


@pytest.mark.parametrize("diagnostic", [None, {"responses": [{"role": "woofer:rear"}]}])
def test_a_round_banks_the_branch_diagnostic_its_analysis_carried(diagnostic):
    """``round_captures._capture_response`` refuses every non-``summed`` role
    without it, and the take record is banked write-once after ``enrich``.
    """
    conductor = _conductor(FakeSeams(measure=lambda program: replace(
        _measure_analysis(program), branch_diagnostic=diagnostic)),
        index_phase_map={1: "measure"}, gain_plan_db=GAINS)
    manifest = RunManifest("branches", SimpleNamespace(bank=AsyncMock(return_value="manifest")))
    records = SimpleNamespace(enrich=None, after_bank=None)
    bind_plan_analysis(conductor, records, manifest=manifest, evidence={})
    manifest.begin({"index": 1, "candidate_id": "candidate", "purpose": "speaker", "purposes": ["speaker"],
                    "pose": {"kind": "bearing", "azimuth_deg": -20, "elevation_deg": 0}},
                   attempt=1, pose_index=0)
    spec = MeasureSpec(kind="baseline", graph_scope="drivers", program_phase="measure")
    program = compose_plan_program(conductor, spec, None)
    banked = records.enrich(
        WiredCaptureAnswer(wav=b"", program=program.to_dict()),
        {"take_id": "take-1", "index": 1, "attempt": 1, "phase": "measure",
         "program": program.to_dict()},
    )
    assert banked["branch_diagnostic"] == diagnostic


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("status", [None, "unmeasured", "too_loud", "too_quiet", "low"])
def test_mic_meter_grade_is_a_disclosure(phase, status):
    verdict = cd.assess(_analysis(mic_meter_status=status), phase=phase)
    assert verdict.ok and verdict.fault is None
    assert verdict.evidence["mic_meter_status"] == (status or "unmeasured")
    assert verdict.capabilities["mic_level"] is (status in {"too_loud", "too_quiet", "low"})


@pytest.mark.parametrize("phase", ["check", "verify"])
def test_clip_auto_retry_comes_from_the_registry_without_a_gain_target(phase):
    take = cd.assess(_analysis(locations=(_loc("sweep_w", clipped=True),)), phase=phase)
    result = refusal_copy.PhaseVerdict.from_take(take)
    assert result.code == refusal_copy.REASON_CLIPPED
    assert refusal_copy.REASON_REGISTRY[result.code].template == refusal_copy.TEMPLATE_SILENT_AUTO_RETRY
    assert result.next == "retake_quieter" and result.next_gain_db is None


@pytest.mark.parametrize("glitch_inputs,frame_loss", [
    ((), False), (("epsilon_out_of_bound",), False), ((), True),
])
def test_repeat_level_is_published_but_clock_and_frame_loss_still_refuse(glitch_inputs, frame_loss):
    drift = DriftEstimate(1000.0 if glitch_inputs else 30.0, 0.2, bool(glitch_inputs),
                          repeat_level_delta_db=0.533, glitch_inputs=glitch_inputs)
    analysis = _analysis(
        glitch_detected=drift.glitch_detected, drift=drift, pilot_snr_ok=not frame_loss,
        frame_ledger=FrameLedger(received_frames=128, declared_frames=256 if frame_loss else 128),
    )
    result = cd.assess(analysis, phase="measure")
    refused = bool(glitch_inputs) or frame_loss
    assert result.ok is not refused
    assert result.fault == ("drift_baselines_disagree" if refused else None)
    assert result.next == ("retake_same" if refused else "accept")
    assert result.charge == ("speaker" if refused else "none")
    assert result.evidence["repeat_level_delta_db"] == analysis_json(analysis)["repeat_level_delta_db"] == 0.533


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("alignment", [_alignment(status="unresolved"), _alignment(delay_us=2000.0), None])
def test_solver_failure_preserves_the_recording(phase, alignment):
    verdict = cd.assess(_analysis(alignment=alignment), phase=phase,
                        priors=MeasurementPriors(alignment_delay_bounds_us=(50.0, 300.0)))
    assert verdict.ok and verdict.fault is None
    assert verdict.capabilities["delay_estimate"] is False
    assert verdict.capabilities["magnitude"] is True
    assert verdict.next == "accept"


@pytest.mark.parametrize("anchor_corroborated", [True, False])
def test_louder_retake_on_a_quieter_role_keeps_the_program_peak(anchor_corroborated):
    band = snr_policy.band_snr_verdicts(
        decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -40}],
        noise_bands=[{"band_id": "mid", "level_dbfs": -70}], noise_floor_dbfs_scalar=None,
        relevant_hz=(1000, 4000), model=DRIVER,
    )
    response = replace(_driver_response("tweeter", 8.0), snr={"alignment": band})
    anchor = AnchorEvidence(corroborated=anchor_corroborated, ambiguous=False)
    verdict = cd.assess(_analysis(driver_responses=(response,), anchor=anchor), phase="measure",
                        gain_db={"woofer": -20.0, "tweeter": -30.0}, gain_ceiling_db={"tweeter": -22.0})
    assert verdict.fault == (None if anchor_corroborated else refusal_copy.REASON_ANCHOR_TOO_QUIET)
    assert verdict.next == "retake_louder"
    assert {
        key.removeprefix("next_gain_db."): float(value)
        for key, value in verdict.evidence.items()
        if key.startswith("next_gain_db.")
    } == {"tweeter": -22.0}
    assert verdict.next_gain_db == -20.0


@pytest.mark.parametrize("cap,volume,expected", [
    (-33.2, -16.9, -16.31), (-8, -16.9, -6), (-33.2, 5, -38.21), (None, -16.9, None),
])
def test_effective_caps_become_digital_gain_ceilings(cap, volume, expected):
    caps = {"tweeter": cap} if cap is not None else {}
    ceilings = cd.capped_gain_ceilings(caps, volume, {"tweeter": -6})
    assert ceilings == ({"tweeter": pytest.approx(expected)} if expected is not None else {})
    response = replace(_driver_response("tweeter", 8), snr={"alignment": {
        "worst_relevant": {"verdict": "insufficient", "band_id": "mid"},
        "bands": [{"band_id": "mid", "shortfall_db": 40}],
    }})
    verdict = cd.assess(_analysis(driver_responses=(response,)), phase="measure", gain_db={"tweeter": -50},
                        gain_ceiling_db={"tweeter": -50}, caps_dbfs=caps, session_volume_db=volume,
                        spl_stop_db_spl=85, spl={"max_window_db_spl": 30, "ceiling_db_spl": 85})
    assert verdict.next_gain_db == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize("cap,volume,session_headroom,spl_headroom,magnitude,raise_db,capped_by,residual", [
    (-33.2, -16.9, 0, 20, "ok", 12, None, None),
    (-8, -16.9, 0, 20, "ok", 12, None, None),
    (-42.89, -16.9, 0, 20, "ok", 4, "driver_cap", 2),
    (-20.99, 5, 0, 20, "ok", 4, "driver_cap", 2),
    (-33.2, -16.9, 0, 3, "ok", 3, "spl_stop", 3),
    (-33.2, -16.9, 0, 0, "ok", 0, "spl_stop", 6),
    (-46.89, -16.9, 0, 20, "ok", 0, "driver_cap", 6),
    (-33.2, -16.9, 4, None, "ok", 4, "spl_unobserved", 6),
    (-33.2, -16.9, 4, float("nan"), "ok", 4, "spl_unobserved", 6),
    (-33.2, -16.9, 4, float("inf"), "ok", 4, "spl_unobserved", 6),
    (None, -16.9, 4, 20, "ok", 0, "ceiling_unavailable", 6),
    (-33.2, -16.9, 0, 20, "insufficient", 0, None, None),
    (-33.2, -16.9, 4, 20, "insufficient", 4, None, None),
])
@pytest.mark.parametrize("stop", [80, 85])
def test_alignment_only_retry_uses_driver_and_spl_headroom(
    cap,
    volume,
    session_headroom,
    spl_headroom,
    magnitude,
    raise_db,
    capped_by,
    residual,
    stop,
):
    band = snr_policy.band_snr_verdicts(
        decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -41}],
        noise_bands=[{"band_id": "mid", "level_dbfs": -70}], noise_floor_dbfs_scalar=None,
        relevant_hz=(1000, 4000), model=DRIVER,
    )
    response = replace(_driver_response("woofer", 8.0),
                       snr={"alignment": band, "worst_relevant": {"verdict": magnitude}})
    verdict = cd.assess(_analysis(driver_responses=(response,)), phase="measure", gain_db=GAINS,
        gain_ceiling_db={"woofer": -30 + session_headroom}, caps_dbfs={"woofer": cap} if cap is not None else {},
        session_volume_db=volume, spl_stop_db_spl=stop,
        spl={"max_window_db_spl": stop - 3 - spl_headroom if spl_headroom is not None else None, "ceiling_db_spl": 85})
    retaken = bool(raise_db)
    assert verdict.ok and verdict.fault is None
    assert (verdict.next, verdict.charge) == (("retake_louder", "speaker") if retaken else ("accept", "none"))
    assert verdict.next_gain_db == (pytest.approx(-30 + raise_db) if retaken else None)
    assert {
        key.removeprefix("next_gain_db."): float(value)
        for key, value in verdict.evidence.items()
        if key.startswith("next_gain_db.")
    } == ({"woofer": pytest.approx(-30 + raise_db)} if raise_db else {})
    assert verdict.capabilities["delay_estimate"] is False
    assert verdict.evidence["alignment.woofer.alignment_level_db"] == -30
    assert verdict.evidence["alignment.woofer.alignment_snr_shortfall_db"] == 6
    assert verdict.evidence.get("alignment.woofer.alignment_level_capped_by") == capped_by
    assert verdict.evidence.get("alignment.woofer.alignment_snr_residual_shortfall_db") == (pytest.approx(residual) if residual is not None else None)


@pytest.mark.parametrize("cap,peak,raise_db,noise_drop_db,capped_by,after", [
    (-37.99, 62, 12, 0, None, 0), (-45.99, 62, 4, 0, "driver_cap", 2),
    (-37.99, 79, 3, 0, "spl_stop", 3), (-45.99, 62, 4, 3, None, 0),
])
@pytest.mark.parametrize("takes", [1, 2], ids=["last-take-replays", "raise-rides-the-next-take"])
async def test_round_retake_banks_played_levels_and_measured_shortfalls(
        takes, cap, peak, raise_db, noise_drop_db, capped_by, after):
    """The last MEASURE take at the mark is retaken at its raise, and the
    retake's record banks its alignment levels with the shortfall its stop's
    first attempt measured before it (ADR-0383 §4, ADR-0395). With a later
    MEASURE take there, the first is kept and the next plays at the raise the
    retake would have played; its record banks its own shortfall (ADR-0433)."""
    def measure(program):
        band = snr_policy.band_snr_verdicts(
            decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000],
                                                        "level_dbfs": -41 + program.segment("sweep_w").gain_db + 30}],
            noise_bands=[{"band_id": "mid", "level_dbfs": -70 - (noise_drop_db if program.segment("sweep_w").gain_db > -30 else 0)}], noise_floor_dbfs_scalar=None,
            relevant_hz=(1000, 4000), model=DRIVER,
        )
        return replace(_measure_analysis(program),
                       driver_responses=(replace(_driver_response("woofer", 8), snr={"alignment": band}),))

    conductor = _conductor(FakeSeams(measure=measure), index_phase_map=dict.fromkeys(range(1, takes + 1), "measure"),
                           gain_plan_db=GAINS, driver_caps_dbfs={"woofer": cap, "tweeter": -30})
    conductor._measure_gain_ceiling_db.update(GAINS)
    manifest = RunManifest("alignment", SimpleNamespace(bank=AsyncMock(side_effect=lambda record: record.get("take_id", "manifest"))))
    records = SimpleNamespace(enrich=None, after_bank=None)
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest, evidence={})
    spec = MeasureSpec(kind="baseline", graph_scope="drivers", program_phase="measure")
    asked = None
    for index, attempt in ((1, 1), (1, 2)) if takes == 1 else ((1, 1), (2, 1)):
        first = (index, attempt) == (1, 1)
        manifest.begin({"index": index, "candidate_id": "candidate", "purpose": "speaker", "purposes": ["speaker"], "pose": {"kind": "bearing", "azimuth_deg": 0, "elevation_deg": 0}},
                       attempt=attempt, pose_index=0)
        program = compose_plan_program(conductor, spec, asked)
        gain = program.segment("sweep_w").gain_db
        assert gain == pytest.approx(-30 + (0 if first else raise_db))
        assert all(seg.effective_peak_dbfs <= conductor._excitation.caps_dbfs[seg.role]
                   for seg in program.stimulus_segments())
        spl = {"max_window_db_spl": peak + gain + 30, "ceiling_db_spl": 85}
        capture = WiredCaptureAnswer(wav=b"", program=program.to_dict(), capture_integrity={"spl": spl})
        record = {"take_id": f"take-{index}-{attempt}", "index": index, "attempt": attempt,
                  "phase": "measure", "program": program.to_dict()}
        records.enrich(capture, record)
        analysis = analyze(record)
        verdict = assessor(analysis, phase="measure", program=program)
        assert (verdict.next, verdict.charge) == (("retake_louder", "speaker") if first and takes == 1 else ("accept", "none"))
        assert verdict.ok and verdict.fault is None
        asked = verdict.next_gain_db
        manifest.judge = AsyncMock(return_value=(verdict, {}))
        record_id = await manifest.bank({**record, "analysis": analysis_json(analysis)})
        (banked, _), = manifest.pending_records
        await manifest.append(banked, record_id, verdict, complete=True, level_observation={})
    rows, _ = round_alignment(manifest.joined(), {})
    pair, = rows
    level = pair["levels"]["woofer"]
    assert level["alignment_level_db"] == pytest.approx(-30 + raise_db)
    assert level["alignment_snr_shortfall_db"] == {"before": 6 if takes == 1 else pytest.approx(after),
                                                   "after": pytest.approx(after)}
    assert level.get("alignment_level_capped_by") == capped_by
    assert level.get("alignment_snr_residual_shortfall_db") == (after if capped_by else None)
    assert pair["snr"]["woofer"]["verdict"] == ("ok" if after == 0 else "insufficient")
    assert [[take["selected"] for take in group["takes"]] for group in manifest.to_dict()["sets"]] == [
        [takes == 2, True]] * 2
    assert conductor._measure_gain_ceiling_db == GAINS


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("pilot_ok", [True, False])
@pytest.mark.parametrize("objective", ["summed_fit_committed", "flat_sum_estimate"])
def test_low_snr_prices_a_louder_take(phase, pilot_ok, objective):
    band = snr_policy.band_snr_verdicts(
        decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -40}],
        noise_bands=[{"band_id": "mid", "level_dbfs": -70}], noise_floor_dbfs_scalar=None,
        relevant_hz=(1000, 4000), model=DRIVER,
    )
    response = replace(_driver_response("woofer", 8.0), snr={"alignment": band})
    from jasper.audio_measurement.program_analysis.model import CrossoverCandidate

    verdict = cd.assess(_analysis(driver_responses=(response,), pilot_snr_ok=pilot_ok, linearity_ok=pilot_ok,
        candidate=CrossoverCandidate({}, "normal", 0, 1, .9, alignment_objective=objective)), phase=phase,
                        gain_db=GAINS, gain_ceiling_db={"woofer": -20.0})
    assert verdict.ok is pilot_ok
    assert verdict.next == "retake_louder" and verdict.next_gain_db == -20.0
    assert type(verdict.next_gain_db) is float
    assert verdict.charge == "speaker"
    assert verdict.evidence["snr.woofer.alignment.shortfall_db"] == 5.0
    assert verdict.evidence["snr.woofer.alignment.estimated_snr_db"] == 30.0


@pytest.mark.parametrize("phase", PHASES)
def test_quiet_pilot_explains_a_false_glitch(phase):
    verdict = cd.assess(_analysis(pilot_snr_ok=False, linearity_ok=False, glitch_detected=True), phase=phase)
    assert verdict.fault == (refusal_copy.REASON_SNR_FLOOR if phase == "check" else refusal_copy.REASON_PILOT_LEVEL_COLLAPSE)
    assert verdict.next == "fix_and_retake"


@pytest.mark.parametrize(("changes", "code"), [
    ({"channel_map_ok": False, "pilot_snr_ok": False}, refusal_copy.REASON_CHANNEL_MAP_MISMATCH),
    ({"gain_plan": None}, refusal_copy.REASON_SNR_FLOOR),
    ({"linearity_ok": False, "gain_plan": GainPlan(GAINS, -30.0, False)}, refusal_copy.REASON_NOISY_ROOM_LINEARITY),
])
def test_check_gates(changes, code):
    assert cd.assess(_analysis(**changes), phase="check").fault == code


@pytest.mark.parametrize("ok", [False, True])
def test_phase_verdict_publishes_take_fields(ok):
    take = cd.assess(_analysis(glitch_detected=not ok), phase="measure")
    result = refusal_copy.PhaseVerdict.from_take(take)
    for field in ("evidence", "capabilities", "next", "next_gain_db", "charge"):
        assert getattr(result, field) == getattr(take, field)


def test_no_household_vocabulary_reaches_this_module():
    """The assessor emits codes; refusal_copy owns household rendering."""
    assert cd.__file__
    tree = ast.parse(Path(cd.__file__).read_text())
    imported: set[str] = set()
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            reached.add(node.attr)
    assert not {"REASON_REGISTRY", "reason_message", "PhaseVerdict"} & (imported | reached)
    assert not any("crossover_v2_flow" in name for name in imported)
    assert not any(name.startswith("jasper.web") for name in imported)


@pytest.mark.parametrize("same_pose,delta,accepted", [(True, 1.9, True), (True, 2.1, False),
    (False, 5.9, True), (False, 6.1, False)])
def test_session_level_drift_has_margin(same_pose, delta, accepted):
    level = cd.level_drift_verdict(loudest_half_second_db_spl=70 + delta, level_reference_db_spl=70, same_pose=same_pose)
    verdict = cd.assess(_analysis(), phase="measure", level_verdict=level)
    assert verdict.ok is accepted
    assert verdict.evidence["loudest_half_second_db_spl"] == 70 + delta
    assert verdict.evidence["level_delta_db"] == pytest.approx(delta)
    if not accepted:
        assert (verdict.fault, verdict.next, verdict.charge) == ("level_drift_at_session_gain", "retake_same", "none")


_LOUDER = refusal_copy.TakeVerdict(False, fault="snr_floor", next="retake_louder", charge="speaker",
                                   next_gain_db=-29.0)


@pytest.mark.parametrize("heard,prior,reading,stop,asked,next_,gain", [
    (True, None, 80.0, 85.0, None, "accept", None), (True, None, 78.5, 85.0, None, "accept", None),
    (True, None, 66.0, 85.0, None, "retake_louder", -27.0), (True, None, 50.0, 85.0, None, "retake_louder", -25.0),
    (True, None, 84.0, 85.0, None, "retake_quieter", -45.0),
    (True, None, 75.0, 80.0, None, "accept", None), (True, None, 80.0, 80.0, None, "retake_quieter", -46.0),
    (True, None, None, 85.0, None, "accept", None),
    (False, None, 45.0, 85.0, None, "fix_and_retake", None),
    (True, _LOUDER, 80.0, 85.0, None, "retake_louder", -40.0),
    (True, None, 75.0, 85.0, -40.0, "retake_louder", -36.0), (True, None, 75.0, 85.0, -35.0, "accept", None),
])
def test_a_near_field_take_is_levelled_toward_its_target(heard, prior, reading, stop, asked, next_, gain):
    """A heard near-field take lands 80 dB (±2), read from its located sweeps
    and never held above the admission bound under its own stop: outside the
    band it is retaken 1:1 at a peak aimed 1 dB under the target, raised at most
    15 dB a step, unless its ceiling already held it under the peak it asked
    for. One the microphone did not hear is judged by its recording, and no
    other retake raises a take past its target (ADR-0361, ADR-0364)."""
    program = build_measure_program({"woofer": -40.0}, (RoleBand("woofer", 0, FrequencyBand(20, 2000)),),
                                    repeat_count=1, sweep_durations={"woofer": 0.2})
    # sens_factor_db -12 reads dBFS + 106 as dB SPL; the floor sits 30 dB under.
    levels = () if reading is None else (LevelReading(-40.0, reading - 106.0, reading - 136.0),)
    analysis = _analysis(stimulus_levels=levels, **({} if heard else {"locations": (_loc("sweep_w", confidence=0.05),)}))
    verdict = cd.assess(analysis, phase="measure", prior_verdict=prior, program=program,
                        spl={"sens_factor_db": -12.0, "ceiling_db_spl": stop},
                        pose_level=SPOT_LEVEL, level_asked_dbfs=asked)
    assert (verdict.next, verdict.next_gain_db) == (next_, gain)
    assert verdict.evidence.get("level_capped", False) is (asked == -35.0)
    if reading is not None:
        assert verdict.evidence["level_db_spl"] == pytest.approx(reading)
    if heard and prior is None and next_ != "accept":
        assert (verdict.ok, verdict.fault, verdict.charge) == (False, "level_off_target", "speaker")


@pytest.mark.parametrize("gains,heard,floor,stopped_at,next_,gain,shortfall", [
    # The burst playing when the stop fired may be cut: the highest one before it solves.
    ((-52.0, -46.0, -40.0, -34.0, -28.0), (58.0, 64.0, 70.0, 74.0), 40.0, 76.0, "retake_louder", -31.0, None),
    # A stop in the first burst leaves only it to solve from.
    ((-52.0,), (81.0,), 40.0, 76.0, "retake_quieter", -54.0, None),
    # A ceiling under the solved gain is said before any take.
    ((-52.0, -46.0, -40.0), (58.0, 64.0, 70.0), 40.0, None, "retake_louder", -31.0, 9.0),
])
def test_a_driver_poses_probe_solves_the_gain_its_take_plays_at(gains, heard, floor, stopped_at, next_, gain,
                                                                shortfall):
    """A probe is solved once, from its highest burst but the one its stop may have
    cut, when that burst stands trusted over the room (ADR-0365, ADR-0411)."""
    program = build_level_probe_program(RoleBand("woofer", 0, FrequencyBand(20, 2000)), gains,
                                        sweep_band_hz=(20.0, 2000.0), gap_s=0.5, downstream_gain_db=0.0, channels=1)
    levels = tuple(LevelReading(g, spl - 106.0, floor - 106.0) for g, spl in zip(gains, heard))
    spl = {"sens_factor_db": -12.0, "ceiling_db_spl": 85.0, **({"stopped_at_db_spl": stopped_at} if stopped_at else {})}
    verdict = cd.assess(_analysis(stimulus_levels=levels), phase="measure", program=program, spl=spl, pose_level=SPOT_LEVEL)
    assert (verdict.next, verdict.next_gain_db) == (next_, gain)
    assert verdict.evidence.get("level_shortfall_db") == shortfall
    # A probe that solved a gain is the take's own level step: no fault, and its next play is free.
    assert (verdict.fault, verdict.charge) == (None, "replay")


@pytest.mark.parametrize("stopped_at,heard,fault,next_,charge", [
    # The SPL watch did not stop it, so it played to its ceiling: the run stops.
    (None, (58.0, 64.0, 70.0), "level_unreachable", "stop", "none"),
    (None, None, "level_unreachable", "stop", "none"),
    # The watch stopped it, so the room was loud: it asks for the microphone again.
    (76.0, (), "snr_floor", "fix_and_retake", "operator"),
    (76.0, None, "locate_failed", "fix_and_retake", "operator"),
])
def test_a_probe_with_no_reading_it_trusts_stops_the_run_only_at_its_ceiling(stopped_at, heard, fault, next_,
                                                                              charge):
    """A probe that reads nothing it trusts over a 65 dB room, or locates no burst
    (``heard`` is None), stops the run once it played every burst up to its take's
    ceiling (ADR-0365, ADR-0422)."""
    gains = (-52.0, -46.0, -40.0)
    program = build_level_probe_program(RoleBand("woofer", 0, FrequencyBand(20, 2000)), gains,
                                        sweep_band_hz=(20.0, 2000.0), gap_s=0.5, downstream_gain_db=0.0, channels=1)
    analysis = ProgramAnalysis(
        phase="measure", stimulus_id="probe",
        locations=tuple(_loc(f"level_probe_{index}", confidence=0.05 if heard is None else 0.9) for index in range(3)),
        stimulus_levels=tuple(LevelReading(g, spl - 106.0, 65.0 - 106.0) for g, spl in zip(gains, heard or ())))
    spl = {"sens_factor_db": -12.0, "ceiling_db_spl": 85.0, **({"stopped_at_db_spl": stopped_at} if stopped_at else {})}
    verdict = cd.assess(analysis, phase="measure", program=program, spl=spl, pose_level=SPOT_LEVEL)
    assert (verdict.ok, verdict.fault, verdict.next, verdict.charge, verdict.next_gain_db) == (
        False, fault, next_, charge, None)


# jts3's seat probe at one spot (#6113): each burst reads its gain + 113.2 dB SPL over a
# 46.7 dB room. With no frame count, a stopped probe leaves out its last reading.
_SEAT_PROBE = {-60.0: 53.2, -54.0: 59.2, -48.0: 65.2, -42.0: 71.2}
_SEAT_PROBE_GAINS = (-60.0, -54.0, -48.0, -42.0, -36.0, -30.0, -25.21)


def _seat_probe_verdict(heard, stopped_in=None, at=0.25):
    program = build_level_probe_program(RoleBand("woofer", 0, FrequencyBand(30, 18000)), _SEAT_PROBE_GAINS,
                                        sweep_band_hz=(30.0, 18000.0), gap_s=0.5, downstream_gain_db=0.0, channels=1)
    analysis = {}
    if stopped_in is not None:
        # The capture runs its post-roll past a stop ``at`` of the way into that burst.
        bursts = [segment for segment in program.segments if segment.kind == KIND_SWEEP]
        cut = next(burst for burst in bursts if burst.gain_db == stopped_in)
        analysis = {"locations": tuple(replace(_loc(burst.segment_id), scheduled_start=burst.start_sample)
                                       for burst in bursts),
                    "frame_ledger": FrameLedger(cut.start_sample + int(cut.n_samples * at)
                                                + int(WIRED_POST_ROLL_S * program.sample_rate_hz))}
    levels = tuple(LevelReading(g, spl - 106.0, 46.7 - 106.0) for g, spl in heard.items())
    spl = {"sens_factor_db": -12.0, "ceiling_db_spl": 85.0, "stopped_at_db_spl": 76.0}
    return cd.assess(_analysis(stimulus_levels=levels, **analysis), phase="measure", program=program, spl=spl,
                     pose_level=SEAT_LEVEL)


@pytest.mark.parametrize("heard,next_,gain,bound", [
    (_SEAT_PROBE, "retake_louder", -40.2, None),
    # A room sound read the -54 burst 6.78 dB loud: the solve moves 0.78 dB, not a step.
    ({**_SEAT_PROBE, -54.0: 65.98}, "retake_louder", -40.98, -54.0),
    # The top step left reads wrong low, 10.3 dB over the room: one step over the right level.
    ({**_SEAT_PROBE, -42.0: 57.0, -36.0: 77.2}, "retake_louder", -34.2, -48.0),
    # Two top steps read wrong low: one step over what the loudest reading solves.
    ({**_SEAT_PROBE, -48.0: 55.0, -42.0: 57.0, -36.0: 77.2}, "retake_louder", -34.2, -54.0),
    # A top step left within 10 dB of the room asks for the microphone again.
    ({**_SEAT_PROBE, -42.0: 50.0, -36.0: 77.2}, "fix_and_retake", None, None),
])
def test_a_probe_solves_from_its_highest_step_never_a_step_over_its_loudest(heard, next_, gain, bound):
    """A stopped probe solves from its highest step left, never more than one probe
    step over what its loudest reading solves, and names that reading when it sets
    the gain (ADR-0411)."""
    verdict = _seat_probe_verdict(heard)
    assert verdict.next == next_
    assert verdict.next_gain_db == (None if gain is None else pytest.approx(gain))
    assert verdict.evidence.get("level_bound_gain_db") == bound


@pytest.mark.parametrize("heard,stopped_in,at,read", [
    # The stop cut the -36 burst before it read: the full -42 burst stays and solves.
    (_SEAT_PROBE, -36.0, 0.25, 71.2),
    # A room sound in the -54 burst then moves nothing.
    ({**_SEAT_PROBE, -54.0: 65.98}, -36.0, 0.25, 71.2),
    # A burst the stop cut after it read is left out.
    (_SEAT_PROBE, -42.0, 0.25, 65.2),
    # Late in a burst, the post-roll runs past the next burst's start: the cut burst still goes.
    (_SEAT_PROBE, -42.0, 0.9, 65.2),
])
def test_a_stopped_probe_leaves_out_only_the_burst_its_stop_cut(heard, stopped_in, at, read):
    """Only the burst playing as the stop fired may have been cut short (ADR-0411)."""
    verdict = _seat_probe_verdict(heard, stopped_in, at)
    assert verdict.evidence["level_db_spl"] == pytest.approx(read)
    assert verdict.next_gain_db == pytest.approx(-40.2)


def test_run_host_passes_the_excitation_caps_and_preset_spl_stop_unchanged(monkeypatch):
    from jasper.web import correction_run_host
    from tests.crossover_v2_fixtures import _run_phase

    conductor = _conductor(
        FakeSeams(), gain_plan_db=GAINS, index_phase_map={1: "measure"}
    )
    excitation = replace(
        conductor.excitation, caps_dbfs={"woofer": -9.25, "tweeter": -51.75}
    )
    conductor.set_excitation(excitation)
    assess = Mock(wraps=correction_run_host.assess)
    monkeypatch.setattr(correction_run_host, "assess", assess)

    _run_phase(conductor, 1, 1)

    inputs = assess.call_args.kwargs
    assert conductor.excitation is excitation
    assert inputs["caps_dbfs"] is excitation.caps_dbfs
    assert inputs["caps_dbfs"] == excitation.caps_dbfs
    assert (
        inputs["spl_stop_db_spl"]
        is conductor.source_preset.safety.max_commissioning_level_db_spl
    )
    assert (
        inputs["spl_stop_db_spl"]
        == conductor.source_preset.safety.max_commissioning_level_db_spl
    )
