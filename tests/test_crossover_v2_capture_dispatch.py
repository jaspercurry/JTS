# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import capture_dispatch as cd, refusal_copy
from jasper.audio_measurement import snr_policy
from jasper.audio_measurement.frame_ledger import FrameLedger
from jasper.audio_measurement.program_analysis.model import (
    AnchorEvidence, DriftEstimate, GainPlan, MeasurementPriors, ProgramAnalysis,
)
from jasper.audio_measurement.quality_model import DRIVER
from tests.crossover_v2_fixtures import (
    FakeSeams, _alignment, _conductor, _driver_response, _loc, _measure_analysis, _run_phase,
)

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


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(("changes", "code", "next", "charge"), [
    ({"locations": ()}, refusal_copy.REASON_LOCATE_FAILED, "fix_and_retake", "operator"),
    ({"anchor_ambiguous": True}, refusal_copy.REASON_ANCHOR_AMBIGUOUS, "fix_and_retake", "operator"),
    ({"anchor": AnchorEvidence(corroborated=False)}, refusal_copy.REASON_ANCHOR_TOO_QUIET, "fix_and_retake", "speaker"),
    ({"locations": (_loc("sweep_w", clipped=True),)}, refusal_copy.REASON_CLIPPED, "retake_quieter", "speaker"),
    ({"glitch_detected": True}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
    ({"frame_ledger": FrameLedger(received_frames=128, declared_frames=256)}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE, "retake_same", "speaker"),
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


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("pilot_ok", [True, False])
def test_low_snr_prices_a_louder_take(phase, pilot_ok):
    band = snr_policy.band_snr_verdicts(
        decision_class="alignment", capture_bands=[{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -40}],
        noise_bands=[{"band_id": "mid", "level_dbfs": -70}], noise_floor_dbfs_scalar=None,
        relevant_hz=(1000, 4000), model=DRIVER,
    )
    response = replace(_driver_response("woofer", 8.0), snr={"alignment": band})
    verdict = cd.assess(_analysis(driver_responses=(response,), pilot_snr_ok=pilot_ok), phase=phase,
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
