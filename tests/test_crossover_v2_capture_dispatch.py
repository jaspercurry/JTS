# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import capture_dispatch as cd, refusal_copy
from jasper.active_speaker.alignment_evidence import round_alignment
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.run_manifest import RunManifest
from jasper.audio_measurement.wired_capture import WiredCaptureAnswer
from jasper.web.correction_run_host import bind_plan_analysis, compose_plan_program
from jasper.audio_measurement import snr_policy
from jasper.audio_measurement.frame_ledger import FrameLedger
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis.model import (
    AnchorEvidence, DriftEstimate, GainPlan, MeasurementPriors, ProgramAnalysis,
)
from jasper.audio_measurement.quality_model import DRIVER
from jasper.platform import control_client
from tests.crossover_v2_fixtures import (
    FakeSeams, _alignment, _conductor, _driver_response, _loc, _measure_analysis, _run_phase,
    _snr_pilot, plan_context,
)
from jasper.cli.measure import _ran
from tests.engine_twin import FakeSeams as EngineSeams, open_session
from tests.test_plan_run import AnsweredGate, _run_gated, _walk

PHASES = ("check", "measure", "verify")
GAINS = {"woofer": -30.0, "tweeter": -30.0}


def _analysis(**changes):
    return replace(ProgramAnalysis(
        phase="measure", program_id="take", locations=(_loc("sweep_w"),),
        pilot_snr_ok=True, linearity_ok=True, channel_map_ok=True,
        anchor=AnchorEvidence(presence=0.5, confidence=0.9, corroborated=True),
        mic_meter_status="usable", alignment=_alignment(),
        driver_responses=(_driver_response("woofer", 8.0),),
        gain_plan=GainPlan(gain_db=GAINS, predicted_peak_dbfs=-30.0, snr_floor_ok=True),
    ), **changes)


@pytest.mark.parametrize("muted", [True, False, None])
async def test_not_heard_take_stops_only_when_output_is_muted(monkeypatch, muted):
    response = control_client.ControlResponse(200, b'{"muted": true, "percent": 0}' if muted else b'{"muted": false, "percent": 35}')
    read = Mock(return_value=response, side_effect=control_client.ControlError() if muted is None else None)
    monkeypatch.setattr(control_client, "get", read)
    analyses = iter((_analysis(locations=()), _analysis()))
    gate = AnsweredGate()
    result, fakes = await _run_gated(_walk([0]), gate=gate, analyze=lambda *_: next(analyses))
    read.assert_called_once_with("/volume")
    assert len(fakes.play.rungs) == (1 if muted else 2)
    assert result.reason == ("measurement_output_muted" if muted else "")
    fault = next(row for row in gate.progress if row.get("fault"))
    assert (fault["fault"], fault["next_action"]) == (
        ("measurement_output_muted", "stop") if muted else ("locate_failed", "fix_and_retake"))
    if muted:
        assert len(gate.grants) == 1


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(("changes", "code", "next", "charge"), [
    ({"locations": ()}, refusal_copy.REASON_LOCATE_FAILED, "fix_and_retake", "operator"),
    ({"anchor_ambiguous": True}, refusal_copy.REASON_ANCHOR_AMBIGUOUS, "fix_and_retake", "operator"),
    ({"anchor": AnchorEvidence(corroborated=False)}, refusal_copy.REASON_ANCHOR_TOO_QUIET, "fix_and_retake", "speaker"),
    ({"locations": (_loc("sweep_w", clipped=True),)}, refusal_copy.REASON_CLIPPED, "retake_quieter", "speaker"),
    ({"glitch_detected": True}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"frame_ledger": FrameLedger(received_frames=128, declared_frames=256)}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"frame_ledger": FrameLedger(received_frames=128, capture_gaps=1, capture_gap_frames=48)}, refusal_copy.REASON_CAPTURE_OVERRUN, "retake_same", "speaker"),
    ({"discontinuity_samples": -1066.7}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"locations": (_loc("sweep_w", residual_samples=1200.0),)}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"linearity_ok": False}, refusal_copy.REASON_AGC_BEHAVIORAL_FAIL, "fix_and_retake", "operator"),
    ({"mic_meter_status": "clipping"}, refusal_copy.REASON_CLIPPED, "retake_quieter", "speaker"),
    ({"mic_meter_status": "too_quiet"}, refusal_copy.REASON_PILOT_LEVEL_COLLAPSE, "fix_and_retake", "operator"),
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
#: One ``event=outputd.xrun``'s worth of inserted samples — 8.3 ms at 48 kHz.
XRUN_SPLICE_SAMPLES = 400.0


def _pair_take(*, rear_role="woofer:rear", branches=True, splice_after=None):
    shift = 0.0
    locations = []
    for segment_id, branch, confidence, residual_ms in TAKE_0003_SWEEPS:
        locations.append(replace(_loc(
            segment_id, confidence=confidence,
            residual_samples=residual_ms * cd.REQUIRED_SAMPLE_RATE_HZ / 1000.0 + shift,
        ), role="woofer" if branch == "front" else rear_role))
        if segment_id == splice_after:
            shift = XRUN_SPLICE_SAMPLES
    return _analysis(
        locations=tuple(locations), pilots=(_snr_pilot("woofer", 30.0),),
        branch_diagnostic={"responses": [{"role": rear_role}]} if branches else None,
    )


@pytest.mark.parametrize(("rear_role", "branches", "accepted"), [
    ("woofer:rear", True, True),
    ("tweeter", True, True),
    ("woofer", True, False),
    ("tweeter", False, False),
])
def test_only_a_branch_programs_unanchored_branch_is_judged_on_its_own_path(rear_role, branches, accepted):
    """The exemption keys on the PROGRAM's shape, never on the driver: the same
    numbers on the anchored branch, or on a program that built no branch
    diagnostic, stay refused.
    """
    verdict = cd.assess(_pair_take(rear_role=rear_role, branches=branches),
                        phase="measure", gain_db=GAINS)
    assert verdict.ok is accepted
    assert verdict.fault == (None if accepted else refusal_copy.REASON_LOCATE_FAILED)
    # Judging a role against its own slot must not edit what was measured.
    assert verdict.evidence["schedule_residual_ms_worst"] == pytest.approx(5.104, abs=1e-3)
    assert verdict.evidence["locate_confidence_min"] == pytest.approx(0.2376)


@pytest.mark.parametrize("splice_after", ["sweep_w", "sweep_w_rep"])
def test_a_spliced_xrun_still_refuses_a_branch_take(splice_after):
    """A splice anywhere in the take moves at least one sweep off its OWN
    branch's slot — after the anchored branch's first sweep, or between the
    unanchored branch's two.
    """
    verdict = cd.assess(_pair_take(splice_after=splice_after), phase="measure", gain_db=GAINS)
    assert not verdict.ok and verdict.fault == refusal_copy.REASON_DRIFT_BASELINES_DISAGREE
    assert (verdict.next, verdict.charge) == ("retake_same", "speaker")
    assert verdict.evidence["guard"] == "sweep_schedule"


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
    manifest.begin({"index": 1, "candidate_id": "candidate",
                    "pose": {"kind": "bearing", "deg": -20, "elevation_deg": 0}},
                   attempt=1, pose_index=0)
    spec = MeasureSpec(kind="baseline", graph_scope="drivers", program_phase="measure")
    program = compose_plan_program(conductor, spec, None, context=plan_context())
    banked = records.enrich(
        WiredCaptureAnswer(wav=b"", program=program.to_dict()),
        {"take_id": "take-1", "index": 1, "attempt": 1, "phase": "measure",
         "program": program.to_dict()},
    )
    assert banked.get("branch_diagnostic") == diagnostic
    assert ("branch_diagnostic" in banked) is (diagnostic is not None)


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("status", [None, "unmeasured", "too_loud"])
def test_absent_clipping_and_unknown_meter_evidence_do_not_refuse(phase, status):
    verdict = cd.assess(_analysis(mic_meter_status=status), phase=phase)
    assert verdict.ok and verdict.fault is None
    assert verdict.evidence["mic_meter_status"] == (status or "unmeasured")
    assert verdict.capabilities["mic_level"] is (status == "too_loud")


@pytest.mark.parametrize("phase", ["check", "verify"])
def test_clip_auto_retry_comes_from_the_registry_without_a_gain_target(phase):
    take = cd.assess(_analysis(locations=(_loc("sweep_w", clipped=True),)), phase=phase)
    result = refusal_copy.PhaseVerdict.from_take(take).to_capture_dict()
    assert result["code"] == refusal_copy.REASON_CLIPPED
    assert result["template"] == refusal_copy.TEMPLATE_SILENT_AUTO_RETRY
    assert result["next"] == "retake_quieter" and result["next_gain_db"] is None
    assert result["auto_retry"] is True


@pytest.mark.parametrize("frame_loss", [False, True])
def test_a_mic_bump_costs_the_operator_but_frame_loss_costs_the_speaker(frame_loss):
    result = cd.assess(_analysis(
        glitch_detected=True, pilot_snr_ok=not frame_loss,
        drift=DriftEstimate(30.0, 0.2, True, glitch_inputs=("repeat_level_disagree",)),
        frame_ledger=FrameLedger(received_frames=128, declared_frames=256 if frame_loss else 128),
    ), phase="measure")
    assert result.charge == ("speaker" if frame_loss else "operator")
    assert not result.ok and result.next == "retake_same"


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
    assert verdict.gain_targets == {"tweeter": -22.0}
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
])
@pytest.mark.parametrize("stop", [80, 85])
def test_alignment_only_retry_uses_driver_and_spl_headroom(cap, volume, session_headroom, spl_headroom, magnitude, raise_db, capped_by, residual, stop):
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
    assert verdict.ok and verdict.fault is None
    assert verdict.next == ("retake_louder" if raise_db else "accept")
    assert verdict.next_gain_db == (pytest.approx(-30 + raise_db) if raise_db else None)
    assert verdict.gain_targets == ({"woofer": pytest.approx(-30 + raise_db)} if raise_db else {})
    assert verdict.capabilities["delay_estimate"] is False
    assert verdict.evidence["alignment.woofer.alignment_level_db"] == -30
    assert verdict.evidence["alignment.woofer.alignment_snr_shortfall_db"] == 6
    assert verdict.evidence.get("alignment.woofer.alignment_level_capped_by") == capped_by
    assert verdict.evidence.get("alignment.woofer.alignment_snr_residual_shortfall_db") == (pytest.approx(residual) if residual is not None else None)


@pytest.mark.parametrize("cap,peak,raise_db,noise_drop_db,capped_by,after", [
    (-37.99, 62, 12, 0, None, 0), (-45.99, 62, 4, 0, "driver_cap", 2),
    (-37.99, 79, 3, 0, "spl_stop", 3), (-45.99, 62, 4, 3, None, 0),
])
async def test_round_retake_banks_played_levels_and_measured_shortfalls(cap, peak, raise_db, noise_drop_db, capped_by, after):
    def measure(program):
        band = snr_policy.band_snr_verdicts(
            decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000],
                                                        "level_dbfs": -41 + program.segment("sweep_w").gain_db + 30}],
            noise_bands=[{"band_id": "mid", "level_dbfs": -70 - (noise_drop_db if program.segment("sweep_w").gain_db > -30 else 0)}], noise_floor_dbfs_scalar=None,
            relevant_hz=(1000, 4000), model=DRIVER,
        )
        return replace(_measure_analysis(program),
                       driver_responses=(replace(_driver_response("woofer", 8), snr={"alignment": band}),))

    conductor = _conductor(FakeSeams(measure=measure), index_phase_map={1: "measure"}, gain_plan_db=GAINS,
                           measure_gain_ceiling_db=GAINS, driver_caps_dbfs={"woofer": cap, "tweeter": -30})
    manifest = RunManifest("alignment", SimpleNamespace(bank=AsyncMock(return_value="manifest")))
    records = SimpleNamespace(enrich=None, after_bank=None)
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest, evidence={})
    spec = MeasureSpec(kind="baseline", graph_scope="drivers", program_phase="measure")
    rung = None
    for attempt in (1, 2):
        manifest.begin({"index": 1, "candidate_id": "candidate", "pose": {"kind": "bearing", "deg": 20, "elevation_deg": 0}},
                       attempt=attempt, pose_index=0)
        program = compose_plan_program(conductor, spec, rung, context=plan_context())
        gain = program.segment("sweep_w").gain_db
        assert gain == pytest.approx(-30 + (raise_db if attempt == 2 else 0))
        assert all(seg.effective_peak_dbfs <= conductor._excitation.caps_dbfs[seg.role]
                   for seg in program.stimulus_segments())
        spl = {"max_window_db_spl": peak + gain + 30, "ceiling_db_spl": 85}
        capture = WiredCaptureAnswer(wav=b"", program=program.to_dict(), capture_integrity={"spl": spl})
        record = {"take_id": f"take-{attempt}", "index": 1, "attempt": attempt,
                  "phase": "measure", "program": program.to_dict()}
        records.enrich(capture, record)
        records.after_bank(record, record["take_id"])
        analysis = analyze(record, record["take_id"])
        verdict = assessor(analysis, phase="measure", program=program)
        assert verdict.next == ("retake_louder" if attempt == 1 else "accept")
        assert verdict.ok and verdict.fault is None
        rung = verdict.next_gain_db
        await manifest.append({**record, "analysis": analysis_json(analysis)}, record["take_id"], verdict,
                              complete=True, started_s=attempt, ended_s=attempt + 1, level_observation={})
    rows, _ = round_alignment(manifest.to_dict(), {})
    pair, = rows
    level = pair["levels"]["woofer"]
    assert level["alignment_level_db"] == pytest.approx(-30 + raise_db)
    assert level["alignment_snr_shortfall_db"] == {"before": 6, "after": pytest.approx(after)}
    assert level.get("alignment_level_capped_by") == capped_by
    assert level.get("alignment_snr_residual_shortfall_db") == (after if capped_by else None)
    assert pair["snr"]["woofer"]["verdict"] == ("ok" if after == 0 else "insufficient")
    assert conductor._measure_gain_ceiling_db == GAINS


@pytest.mark.parametrize("stop,cap,raise_db,capped_by", [(80, -8, 3, "spl_stop"), (85, -8, 8, None), (85, -42.89, 4, "driver_cap")])
async def test_measure_cli_retries_with_declared_caps_and_each_takes_spl(stop, cap, raise_db, capped_by):
    conductor = _conductor(FakeSeams(), driver_caps_dbfs={"woofer": cap, "tweeter": -33.2})
    excitation = replace(conductor._excitation, session_volume_db=-16.9)
    box = SimpleNamespace(caps_dbfs=excitation.caps_dbfs, session_volume_db=excitation.session_volume_db,
                          preset=replace(conductor._preset, safety=replace(conductor._preset.safety, max_commissioning_level_db_spl=stop)))
    fakes = EngineSeams()
    fakes.volume.proven_db = box.session_volume_db
    manifest = RunManifest("cli", fakes.records)

    async def bank(record):
        rung = fakes.play.rungs[-1]
        program = excitation.measure_program(dict.fromkeys(GAINS, -30 if rung is None else rung))
        return await manifest.bank({**record, "program": program.to_dict(), "capture_integrity": {"spl": {
            "max_window_db_spl": 74 + program.segment("sweep_w").gain_db + 30, "ceiling_db_spl": 85}}})

    def analyze(record, record_id):
        gain = ExcitationProgram.from_dict(record["program"]).segment("sweep_w").gain_db
        band = snr_policy.band_snr_verdicts(
            decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -41 + gain + 30}],
            noise_bands=[{"band_id": "mid", "level_dbfs": -70}], noise_floor_dbfs_scalar=None,
            relevant_hz=(1000, 4000), model=DRIVER,
        )
        return _analysis(driver_responses=(replace(_driver_response("woofer", 8), snr={"alignment": band}),))

    spec = MeasureSpec(kind="baseline", graph_scope="drivers", program_phase="measure", positions=(20,))
    async with open_session(replace(fakes, records=SimpleNamespace(bank=bank)),
                            measurement_level_db=box.session_volume_db, allocate_take_id=manifest.allocate_take_id) as (session, _):
        result = await _ran(session, (spec,), spl_monitor="wired", manifest=manifest, analyze=analyze, box=box)
    assert result.status == "complete"
    assert fakes.play.rungs == [None, pytest.approx(-30 + raise_db)]
    last = next(take for take in result.takes if take["attempt"] == 2 and take["role"] == "woofer")
    level = last["alignment"]["woofer"]
    assert level["alignment_level_db"] == pytest.approx(-30 + raise_db)
    assert level["alignment_snr_shortfall_db"] == {"before": 6, "after": pytest.approx(max(0, 6 - raise_db))}
    assert level.get("alignment_level_capped_by") == capped_by


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

    verdict = cd.assess(_analysis(driver_responses=(response,), pilot_snr_ok=pilot_ok,
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
    verdict = cd.assess(_analysis(pilot_snr_ok=False, glitch_detected=True), phase=phase)
    assert verdict.fault == (refusal_copy.REASON_SNR_FLOOR if phase == "check" else refusal_copy.REASON_PILOT_LEVEL_COLLAPSE)
    assert verdict.next == "fix_and_retake"


@pytest.mark.parametrize(("changes", "code"), [
    ({"channel_map_ok": False, "pilot_snr_ok": False}, refusal_copy.REASON_CHANNEL_MAP_MISMATCH),
    ({"gain_plan": None}, refusal_copy.REASON_SNR_FLOOR),
    ({"delta_implausible": True}, refusal_copy.REASON_ANCHOR_AMBIGUOUS),
    ({"linearity_ok": False, "gain_plan": GainPlan(GAINS, -30.0, False)}, refusal_copy.REASON_NOISY_ROOM_LINEARITY),
])
def test_check_gates(changes, code):
    assert cd.assess(_analysis(**changes), phase="check").fault == code


@pytest.mark.parametrize("has_drivers", [False, True])
def test_measure_purpose_decides_whether_delay_is_required(has_drivers):
    analysis = _analysis(alignment=_alignment(status="unresolved"))
    if not has_drivers:
        analysis = replace(analysis, driver_responses=(), summed_response=_driver_response("summed", 8.0))
    take = cd.assess(analysis, phase="measure")
    assert take.ok and take.fault is None
    assert flow._measure_sufficient(take, analysis) is (not has_drivers)


@pytest.mark.parametrize("ok", [False, True])
def test_phase_verdict_publishes_take_fields(ok):
    take = cd.assess(_analysis(glitch_detected=not ok), phase="measure")
    result = refusal_copy.PhaseVerdict.from_take(take).to_capture_dict()
    for field in ("evidence", "capabilities", "next", "next_gain_db", "charge"):
        assert result[field] == getattr(take, field)


@pytest.mark.parametrize(("ripple", "alignment", "due"), [(15.1, True, True), (15.0, True, False), (99, False, False)])
def test_ripple_is_a_disclosure(ripple, alignment, due):
    assert cd.ripple_reservation_due(predicted_ripple_db=ripple, has_alignment=alignment, disclosure_threshold_db=15.0) is due


@pytest.mark.parametrize(
    ("fault", "code", "figures"),
    [
        ({}, None, {}),
        ({"linearity_ok": False}, refusal_copy.REASON_AGC_BEHAVIORAL_FAIL, {}),
        ({"anchor": AnchorEvidence(presence=0.0156, confidence=0.18, corroborated=False)}, refusal_copy.REASON_ANCHOR_TOO_QUIET,
         {"anchor_presence": 0.0156, "anchor_confidence": 0.18, "anchor_corroborated": False}),
        ({"glitch_detected": True, "discontinuity_samples": -1066.7,
          "drift": DriftEstimate(-3106.0, 1066.7, True, discontinuity_samples=-1066.7)},
         refusal_copy.REASON_DRIFT_BASELINES_DISAGREE,
         {"epsilon_ppm": -3106.0, "max_residual_samples": 1066.7,
          "discontinuity_samples": -1066.7}),
        ({"clipped": True}, refusal_copy.REASON_CLIPPED, {"peak_dbfs": -12.0}),
    ],
)
def test_measure_evidence_reaches_capture_result_and_journal(monkeypatch, fault, code, figures):
    events = []
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_v2.diagnostics.log_event",
        lambda logger, event, **fields: events.append((event, fields)),
    )
    fault = dict(fault)
    clipped = fault.pop("clipped", False)
    fakes = FakeSeams(measure=lambda program: replace(
        _measure_analysis(program, clipped=clipped), mic_meter_status="usable", **fault,
    ))
    conductor = _conductor(fakes)
    _run_phase(conductor, 1, 1)
    verdict = _run_phase(conductor, 2, 1)
    assert verdict["accepted"] is (code is None)
    assert verdict.get("code") == code
    assert verdict["evidence"].items() >= {"mic_meter_status": "usable", **figures}.items()
    for key, value in figures.items():
        assert type(verdict["evidence"][key]) is type(value)
    journal = next(fields for event, fields in events
                   if event == "correction.crossover_v2_measure_diag")
    assert journal["evidence"] == verdict["evidence"]


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
    assert not {"REASON_REGISTRY", "reason_message", "TRANSIENT_AUTO_RETRY_CODES", "PhaseVerdict"} & (imported | reached)
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
