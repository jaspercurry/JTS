"""L2 calibrated crossover alignment — cal-curve application, the phase_aware gate,
and the measured polarity proposal.

These pin the things the L2 increment must get right and that no other test covers:

  - **Cal-curve application**: a calibrated mic's correction curve is actually
    applied to the measured magnitude (provable via the null-depth shift), reusing
    ``jasper.audio_measurement.calibration.apply_calibration_curve`` — not just a quality-gate bool.
  - **The phase_aware gate**: a polarity decision is granted ONLY with a calibrated
    mic. ``resolve_measurement_mode`` is downgrade-only, an uncalibrated capture
    yields an unauthorized proposal, and the data-layer gate in
    ``build_crossover_alignment_proposal`` refuses phase_aware on uncalibrated
    records even when it is requested.
  - **The polarity proposal**: judged from the reverse-vs-in-phase null MARGIN
    (cap-independent), with single-capture fallbacks; the delay is reported as a
    STATUS from the in-phase null (the VALUE is the deferred timing-locked walk's
    job — a per-driver arrival delta from un-synced browser captures is jitter,
    not time-of-flight). Attenuation-only, never an Fc/slope rewrite.

The deconvolution DSP itself is covered by ``test_active_speaker_driver_acoustics``;
here we synthesize IRs (delta / low-pass / comb), convolve the reference sweep, and
assert the L2-specific behaviour.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import fftconvolve, firwin

from jasper.active_speaker import crossover_alignment as ca
from jasper.active_speaker import driver_acoustics as da
from jasper.active_speaker.commissioning_capture import (
    build_crossover_alignment_proposal,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement import sweep as sweep_mod
from jasper.audio_measurement.calibration import CalibrationCurve

from tests.test_active_speaker_profile import _two_way_preset

SR = 48000


def _two_way() -> ActiveSpeakerPreset:
    # Mono 2-way: woofer=lower, tweeter=upper, crossover at 1600 Hz.
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


def _reference_sweep(duration_s: float = 1.0):
    sig, meta = sweep_mod.synchronized_swept_sine(
        f1=da.DEFAULT_F1_HZ,
        f2=da.DEFAULT_F2_HZ,
        duration_approx_s=duration_s,
        sample_rate=SR,
        amplitude_dbfs=da.DEFAULT_AMPLITUDE_DBFS,
    )
    return sig, meta.to_dict()


def _write_capture(tmp_path, name, signal):
    path = tmp_path / name
    sweep_mod.write_sweep_wav(path, signal.astype(np.float32), SR)
    return path


# ===========================================================================
# resolve_measurement_mode — the gate
# ===========================================================================


def test_phase_aware_requires_a_calibrated_mic():
    granted = ca.resolve_measurement_mode("phase_aware", has_calibrated_mic=True)
    assert granted.mode == ca.PHASE_AWARE
    assert granted.downgraded is False
    assert granted.phase_aware is True


def test_phase_aware_downgrades_without_a_calibrated_mic():
    blocked = ca.resolve_measurement_mode("phase_aware", has_calibrated_mic=False)
    assert blocked.mode == ca.MAGNITUDE_ONLY
    assert blocked.downgraded is True
    assert blocked.reason == "no_calibrated_mic"
    assert blocked.phase_aware is False


def test_mode_never_upgrades_and_unknown_is_magnitude_only():
    same = ca.resolve_measurement_mode("magnitude_only", has_calibrated_mic=True)
    assert same.mode == ca.MAGNITUDE_ONLY
    assert same.downgraded is False
    assert ca.resolve_measurement_mode("nonsense", has_calibrated_mic=True).mode == (
        ca.MAGNITUDE_ONLY
    )
    assert ca.resolve_measurement_mode(None, has_calibrated_mic=False).mode == (
        ca.MAGNITUDE_ONLY
    )


# ===========================================================================
# propose_crossover_alignment — gated polarity + delay status
# ===========================================================================


def _propose(mode="phase_aware", **kw):
    return ca.propose_crossover_alignment(
        mode=mode, crossover_fc_hz=1600.0, lower_role="woofer",
        upper_role="tweeter", **kw,
    )


def test_magnitude_only_proposal_is_unauthorized_no_phase_decision():
    # A phone (magnitude_only) can NEVER authorize a polarity/delay decision.
    p = _propose(mode=ca.MAGNITUDE_ONLY, in_phase_null_depth_db=18.0)
    assert p.authorized is False
    assert p.polarity == "normal"
    assert p.polarity_action == ca.POLARITY_REVIEW
    assert p.delay_status == ca.DELAY_UNKNOWN
    assert any(i["code"] == "requires_calibrated_mic" for i in p.issues)


def test_polarity_keep_when_reverse_null_is_much_deeper():
    # Both captures, margin >= POLARITY_MARGIN_DB → correct polarity. Flat in-phase
    # → delay aligned.
    p = _propose(in_phase_null_depth_db=2.0, reverse_null_depth_db=16.0)
    assert p.authorized is True
    assert p.polarity_action == ca.POLARITY_KEEP
    assert p.polarity == "normal"
    assert p.polarity_margin_db == pytest.approx(14.0)
    assert p.delay_status == ca.DELAY_ALIGNED


def test_polarity_invert_when_in_phase_null_is_much_deeper():
    # in-phase ≫ reverse → out of phase → propose a flip. Deep in-phase → delay
    # needs alignment.
    p = _propose(in_phase_null_depth_db=15.0, reverse_null_depth_db=2.0)
    assert p.polarity_action == ca.POLARITY_INVERT
    assert p.polarity == "invert_tweeter"
    assert p.polarity_margin_db == pytest.approx(-13.0)
    assert p.delay_status == ca.DELAY_NEEDS_ALIGNMENT
    assert any(i["code"] == "polarity_inverted_evidence" for i in p.issues)


def test_polarity_review_when_margin_is_small():
    # Neither polarity cancels clearly more than the other → don't decide.
    p = _propose(in_phase_null_depth_db=10.0, reverse_null_depth_db=12.0)
    assert p.polarity_action == ca.POLARITY_REVIEW
    assert p.polarity == "normal"
    assert any(i["code"] == "polarity_ambiguous" for i in p.issues)


def test_margin_is_cap_independent():
    # The relative margin is what matters, not the absolute depth: a shallow-but-
    # clearly-deeper reverse null (both capped low) still reads "keep" — the whole
    # point of going relative instead of an absolute 20/25 dB gate the measurement
    # may never reach.
    p = _propose(in_phase_null_depth_db=1.0, reverse_null_depth_db=12.0)
    assert p.polarity_action == ca.POLARITY_KEEP


def test_in_phase_only_deep_null_is_an_invert_candidate():
    p = _propose(in_phase_null_depth_db=18.0)
    assert p.polarity_action == ca.POLARITY_INVERT
    assert p.polarity == "invert_tweeter"
    assert p.polarity_margin_db is None
    assert any(i["code"] == "summed_null_detected" for i in p.issues)


def test_in_phase_only_flat_keeps_tentatively():
    p = _propose(in_phase_null_depth_db=2.0)
    assert p.polarity_action == ca.POLARITY_KEEP
    assert p.delay_status == ca.DELAY_ALIGNED
    assert any(i["code"] == "reverse_null_not_captured" for i in p.issues)


def test_reverse_only_null_keeps_tentatively():
    p = _propose(reverse_null_depth_db=14.0)
    assert p.polarity_action == ca.POLARITY_KEEP
    assert any(i["code"] == "polarity_tentative_from_reverse" for i in p.issues)


def test_reverse_only_absent_null_reads_review():
    p = _propose(reverse_null_depth_db=1.0)
    assert p.polarity_action == ca.POLARITY_REVIEW
    assert any(i["code"] == "reverse_null_absent" for i in p.issues)


def test_no_summed_evidence_reads_review_and_unknown_delay():
    p = _propose()
    assert p.polarity_action == ca.POLARITY_REVIEW
    assert p.delay_status == ca.DELAY_UNKNOWN
    assert any(i["code"] == "no_summed_capture" for i in p.issues)


def test_delay_status_unknown_when_no_in_phase_capture():
    # Only a reverse capture: the in-phase null (the delay signal) is absent.
    p = _propose(reverse_null_depth_db=14.0)
    assert p.delay_status == ca.DELAY_UNKNOWN


def test_proposal_round_trips_to_dict():
    d = _propose(in_phase_null_depth_db=2.0, reverse_null_depth_db=16.0).to_dict()
    assert d["authorized"] is True
    assert d["polarity_action"] == ca.POLARITY_KEEP
    assert d["delay_status"] == ca.DELAY_ALIGNED
    assert d["polarity_margin_db"] == pytest.approx(14.0)
    assert "delay_ms" not in d  # the delay VALUE is the deferred walk's job
    assert d["kind"] == "jts_active_speaker_crossover_alignment"


# ===========================================================================
# analyze_driver_capture — calibration curve + surfaced FR (no arrival)
# ===========================================================================


def test_driver_capture_surfaces_fr_curve_and_calibrated_flag(tmp_path):
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    curve = CalibrationCurve(freqs_hz=[20.0, 20000.0], correction_db=[0.0, 0.0])
    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 400.0), calibration=curve
    )
    assert result.verdict == "present"
    assert result.calibrated is True
    assert result.fr_curve is not None
    assert len(result.fr_curve["freqs_hz"]) == len(result.fr_curve["mag_db"]) > 2
    assert max(result.fr_curve["mag_db"]) == pytest.approx(0.0, abs=1e-6)
    d = result.to_dict()
    assert d["calibrated"] is True
    assert "arrival_s" not in d  # arrival removed (cross-capture timing not locked)


def test_uncalibrated_driver_capture_is_not_marked_calibrated(tmp_path):
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.calibrated is False


def test_calibration_curve_is_applied_to_the_null_depth(tmp_path):
    """A non-flat calibration curve shifts the measured magnitude — proven via the
    null depth (shoulders at fc/2 and fc*2 see different corrections, so an additive
    cal does NOT cancel there). A step cal that adds +12 dB only at the upper
    shoulder lifts the shoulder mean by ~6 dB → null depth +6 dB."""
    sig, meta = _reference_sweep()
    ir = np.zeros(256, dtype=np.float64)
    ir[10] = 1.0  # delta IR → flat magnitude → ~0 dB null
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "flat.wav", captured)

    fc = 2000.0
    step = CalibrationCurve(
        freqs_hz=[20.0, fc * 1.4, fc * 1.6, 20000.0],
        correction_db=[0.0, 0.0, 12.0, 12.0],
    )
    without = da.analyze_summed_crossover(path, meta, crossover_fc_hz=fc)
    withcal = da.analyze_summed_crossover(
        path, meta, crossover_fc_hz=fc, calibration=step
    )
    assert without.calibrated is False
    assert withcal.calibrated is True
    assert withcal.null_depth_db == pytest.approx(without.null_depth_db + 6.0, abs=1.0)


# ===========================================================================
# analyze_summed_crossover — reverse-polarity (expect_null) interpretation
# ===========================================================================


def test_reverse_polarity_deep_null_is_a_pass(tmp_path):
    sig, meta = _reference_sweep()
    # Comb null at 2 kHz: the deep cancellation a correct reverse-polarity capture
    # WANTS — expect_null flips its meaning from "problem" to "pass".
    ir = np.zeros(64, dtype=np.float64)
    ir[0] = 1.0
    ir[12] = 0.98
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "reverse_null.wav", captured)

    result = da.analyze_summed_crossover(
        path, meta, crossover_fc_hz=2000.0, expect_null=True,
    )
    assert result.expect_null is True
    assert result.null_depth_db >= da.DEFAULT_NULL_THRESHOLD_DB
    assert result.verdict == da.SUMMED_BLEND_OK  # the deep null is the pass


def test_reverse_polarity_shallow_null_is_a_problem(tmp_path):
    sig, meta = _reference_sweep()
    ir = np.zeros(256, dtype=np.float64)
    ir[10] = 1.0  # flat → no null where the reverse capture expected one
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "no_null.wav", captured)

    result = da.analyze_summed_crossover(
        path, meta, crossover_fc_hz=2000.0, expect_null=True,
    )
    assert result.verdict == da.SUMMED_POLARITY_OR_DELAY_PROBLEM


# ===========================================================================
# build_crossover_alignment_proposal — the data-layer gate + state read
# ===========================================================================


def _driver_record(role, *, calibrated, group="mono"):
    return {
        "speaker_group_id": group,
        "role": role,
        "captured": True,
        "acoustic": {"verdict": "present", "calibrated": calibrated},
    }


def _state(*, calibrated, in_phase_null=None, reverse_null=None, group="mono"):
    state = {
        "latest_by_target": {
            f"{group}:woofer": _driver_record("woofer", calibrated=calibrated, group=group),
            f"{group}:tweeter": _driver_record("tweeter", calibrated=calibrated, group=group),
        },
        "latest_summed_by_group": {},
    }
    if in_phase_null is not None or reverse_null is not None:
        is_reverse = reverse_null is not None
        state["latest_summed_by_group"][group] = {
            "speaker_group_id": group,
            "acoustic": {
                "null_depth_db": reverse_null if is_reverse else in_phase_null,
                "expect_null": is_reverse,
                "calibrated": calibrated,
            },
        }
    return state


def test_build_proposal_authorizes_phase_aware_on_calibrated_records():
    state = _state(calibrated=True, in_phase_null=2.0)
    out = build_crossover_alignment_proposal(
        _two_way(), state, requested_mode=ca.PHASE_AWARE
    )
    assert out["status"] == "ok"
    assert out["mode"]["mode"] == ca.PHASE_AWARE
    assert out["proposal"]["authorized"] is True
    assert out["proposal"]["delay_status"] == ca.DELAY_ALIGNED


def test_build_proposal_refuses_phase_aware_on_uncalibrated_records():
    # The data-layer gate: even when phase_aware is REQUESTED, uncalibrated captures
    # downgrade it — a phone can never yield a polarity decision.
    state = _state(calibrated=False, in_phase_null=2.0)
    out = build_crossover_alignment_proposal(
        _two_way(), state, requested_mode=ca.PHASE_AWARE
    )
    assert out["mode"]["mode"] == ca.MAGNITUDE_ONLY
    assert out["mode"]["downgraded"] is True
    assert out["proposal"]["authorized"] is False


def test_build_proposal_reads_reverse_null_from_state():
    state = _state(calibrated=True, reverse_null=14.0)
    out = build_crossover_alignment_proposal(
        _two_way(), state, requested_mode=ca.PHASE_AWARE
    )
    proposal = out["proposal"]
    assert proposal["reverse_null_depth_db"] == 14.0
    assert proposal["polarity_action"] == ca.POLARITY_KEEP


def test_build_proposal_graceful_on_empty_state():
    out = build_crossover_alignment_proposal(
        _two_way(), {"latest_by_target": {}, "latest_summed_by_group": {}},
        requested_mode=ca.PHASE_AWARE,
    )
    assert out["status"] == "no_measurements"
    assert out["proposal"] is None
