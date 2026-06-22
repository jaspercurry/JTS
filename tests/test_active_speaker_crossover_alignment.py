"""L2 calibrated crossover alignment — cal-curve application, the phase_aware gate,
and the measured delay/polarity proposal.

These pin the three things the L2 increment must get right and that no other test
covers:

  - **Cal-curve application**: a calibrated mic's correction curve is actually
    applied to the measured magnitude (provable via the null-depth shift), reusing
    ``correction.calibration.apply_calibration_curve`` — not just a quality-gate
    bool.
  - **The phase_aware gate**: a phase/delay/polarity decision is granted ONLY with
    a calibrated mic. ``resolve_measurement_mode`` is downgrade-only, an
    uncalibrated capture yields an unauthorized proposal, and the data-layer gate
    in ``build_crossover_alignment_proposal`` refuses phase_aware on uncalibrated
    records even when it is requested.
  - **The proposal**: delay whichever driver arrives EARLIER (measured from the IR
    arrival), polarity from the reverse-polarity null proof — attenuation-only,
    never an Fc/slope rewrite, always a human-confirmed proposal.

The deconvolution DSP itself is covered by ``test_active_speaker_driver_acoustics``;
here we synthesize IRs (delta / pure-delay / low-pass), convolve the reference
sweep, and assert the L2-specific behaviour.
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
from jasper.correction import sweep as sweep_mod
from jasper.correction.calibration import CalibrationCurve

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
    # magnitude_only stays magnitude_only even WITH a calibrated mic (never upgrade).
    same = ca.resolve_measurement_mode("magnitude_only", has_calibrated_mic=True)
    assert same.mode == ca.MAGNITUDE_ONLY
    assert same.downgraded is False
    # garbage / empty → magnitude_only, not an error.
    assert ca.resolve_measurement_mode("nonsense", has_calibrated_mic=True).mode == (
        ca.MAGNITUDE_ONLY
    )
    assert ca.resolve_measurement_mode(None, has_calibrated_mic=False).mode == (
        ca.MAGNITUDE_ONLY
    )


# ===========================================================================
# propose_crossover_alignment — gated, measured delay + polarity
# ===========================================================================


def test_magnitude_only_proposal_is_unauthorized_no_phase_decision():
    # A phone (magnitude_only) can NEVER authorize a delay/polarity decision.
    p = ca.propose_crossover_alignment(
        mode=ca.MAGNITUDE_ONLY,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.010,
        upper_arrival_s=0.0120,
        in_phase_null_depth_db=18.0,
    )
    assert p.authorized is False
    assert p.delay_ms is None
    assert p.delay_target_role is None
    assert p.polarity == "normal"
    assert any(i["code"] == "requires_calibrated_mic" for i in p.issues)


def test_delay_targets_the_earlier_arriver_woofer_when_tweeter_is_later():
    # Horn case: the tweeter arrives LATER → delay the woofer (the earlier source),
    # NOT the reflexive "delay the tweeter".
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.0100,
        upper_arrival_s=0.0106,  # tweeter 0.6 ms later
        in_phase_null_depth_db=1.0,
        reverse_null_depth_db=27.0,
    )
    assert p.authorized is True
    assert p.delay_target_role == "woofer"
    assert p.delay_ms == pytest.approx(0.6, abs=0.05)
    assert p.delay_confidence == "estimate"


def test_delay_targets_the_tweeter_when_it_arrives_earlier():
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.0106,
        upper_arrival_s=0.0100,  # tweeter 0.6 ms earlier
    )
    assert p.delay_target_role == "tweeter"
    assert p.delay_ms == pytest.approx(0.6, abs=0.05)


def test_delay_within_jitter_reads_aligned_no_delay():
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.010000,
        upper_arrival_s=0.010001,  # ~0.02 ms, below the jitter floor
    )
    assert p.delay_confidence == "aligned"
    assert p.delay_ms == 0.0
    assert p.delay_target_role is None


def test_delay_is_clamped_to_the_emit_ceiling():
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.0,
        upper_arrival_s=0.050,  # 50 ms apart
    )
    assert p.delay_ms == ca.MAX_DELAY_MS  # never exceeds the 0..20 ms contract


def test_no_arrivals_means_no_delay_proposal():
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        in_phase_null_depth_db=1.0,
        reverse_null_depth_db=26.0,
    )
    assert p.delay_confidence == "none"
    assert p.delay_target_role is None


def test_polarity_keep_when_in_phase_flat_and_reverse_null_deep():
    # The textbook pass: flat in-phase sum + a deep reverse-polarity null.
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.010,
        upper_arrival_s=0.010,
        in_phase_null_depth_db=2.0,
        reverse_null_depth_db=28.0,
    )
    assert p.polarity_action == ca.POLARITY_KEEP
    assert p.polarity == "normal"
    assert p.reverse_verdict == ca.REVERSE_STRONG
    assert p.summed_blend == ca.BLEND_FLAT


def test_polarity_invert_candidate_on_deep_in_phase_null_without_reverse():
    # Deep in-phase null and no reverse proof: propose a flip CANDIDATE, ask for the
    # reverse capture, require confirm — never a silent flip.
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.010,
        upper_arrival_s=0.010,
        in_phase_null_depth_db=18.0,
    )
    assert p.polarity_action == ca.POLARITY_INVERT
    assert p.polarity == "invert_tweeter"
    assert any(i["code"] == "summed_null_detected" for i in p.issues)


def test_polarity_invert_when_in_phase_nulls_and_reverse_is_flat():
    # In-phase nulls (drivers cancel) AND the reverse capture does NOT null (it
    # sums) → the branches are out of phase in the current config → propose a flip.
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.010,
        upper_arrival_s=0.010,
        in_phase_null_depth_db=18.0,
        reverse_null_depth_db=2.0,  # reverse sums flat → invert
    )
    assert p.polarity_action == ca.POLARITY_INVERT
    assert p.polarity == "invert_tweeter"
    assert any(i["code"] == "polarity_inverted_evidence" for i in p.issues)


def test_polarity_review_when_both_polarities_null():
    # Both polarities null — contradictory (a notch from some other cause); don't
    # decide, flag for investigation.
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.010,
        upper_arrival_s=0.010,
        in_phase_null_depth_db=18.0,
        reverse_null_depth_db=28.0,  # BOTH deep nulls → contradictory
    )
    assert p.polarity_action == ca.POLARITY_REVIEW
    assert p.polarity == "normal"
    assert any(i["code"] == "polarity_ambiguous" for i in p.issues)


def test_proposal_round_trips_to_dict():
    p = ca.propose_crossover_alignment(
        mode=ca.PHASE_AWARE,
        crossover_fc_hz=1600.0,
        lower_role="woofer",
        upper_role="tweeter",
        lower_arrival_s=0.010,
        upper_arrival_s=0.0106,
        in_phase_null_depth_db=2.0,
        reverse_null_depth_db=27.0,
    )
    d = p.to_dict()
    assert d["authorized"] is True
    assert d["delay_target_role"] == "woofer"
    assert d["polarity_action"] == ca.POLARITY_KEEP
    assert d["kind"] == "jts_active_speaker_crossover_alignment"


# ===========================================================================
# analyze_driver_capture — calibration curve, arrival, surfaced FR
# ===========================================================================


def test_driver_capture_surfaces_arrival_fr_curve_and_calibrated_flag(tmp_path):
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
    assert result.arrival_s is not None and result.arrival_s >= 0.0
    assert result.fr_curve is not None
    assert len(result.fr_curve["freqs_hz"]) == len(result.fr_curve["mag_db"]) > 2
    # A surfaced curve is a relative shape: 0 dB at its peak.
    assert max(result.fr_curve["mag_db"]) == pytest.approx(0.0, abs=1e-6)
    # to_dict carries the new evidence.
    assert result.to_dict()["calibrated"] is True
    assert result.to_dict()["arrival_s"] == result.arrival_s


def test_uncalibrated_driver_capture_is_not_marked_calibrated(tmp_path):
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.calibrated is False


def test_silent_capture_nulls_the_arrival(tmp_path):
    sig, meta = _reference_sweep()
    ir = (firwin(1023, 400, fs=SR) * 0.002).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "silent.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "silent"
    # A silent IR peak is noise — never reported as an arrival.
    assert result.arrival_s is None


def test_arrival_difference_recovers_a_pure_delay(tmp_path):
    """Two captures that differ only by a pure delay recover that delay in the
    relative arrival — the basis for the measured delay estimate."""
    sig, meta = _reference_sweep()

    def _delayed(delay_samples: int):
        ir = np.zeros(delay_samples + 64, dtype=np.float64)
        ir[delay_samples] = 1.0
        captured = fftconvolve(sig.astype(np.float64), ir)
        return _write_capture(tmp_path, f"d{delay_samples}.wav", captured)

    early = da.analyze_driver_capture(
        _delayed(200), meta, passband_hz=(40.0, 18000.0)
    )
    late = da.analyze_driver_capture(
        _delayed(248), meta, passband_hz=(40.0, 18000.0)  # 48 samples = 1.0 ms later
    )
    assert early.arrival_s is not None and late.arrival_s is not None
    assert (late.arrival_s - early.arrival_s) == pytest.approx(48 / SR, abs=2 / SR)


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
    # +12 dB step that lands ABOVE fc (so fc/2=1000 and fc=2000 see 0, fc*2=4000
    # sees +12). Cal points bracket the transition at fc*1.4 / fc*1.6.
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
    # Comb null at 2 kHz (the deep cancellation a correct reverse-polarity capture
    # WANTS) — expect_null flips its meaning from "problem" to "pass".
    ir = np.zeros(64, dtype=np.float64)
    ir[0] = 1.0
    ir[12] = 0.98
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "reverse_null.wav", captured)

    # The smoothed-shoulder measurement of a synthesized comb caps near ~18 dB, so
    # we exercise the expect_null MECHANISM at the default detection threshold; the
    # production reverse gate (REVERSE_NULL_MIN_DB) is applied by the bridge.
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


def _driver_record(role, arrival_s, *, calibrated, group="mono"):
    return {
        "speaker_group_id": group,
        "role": role,
        "captured": True,
        "acoustic": {
            "verdict": "present",
            "arrival_s": arrival_s,
            "calibrated": calibrated,
        },
    }


def _state(
    *,
    calibrated,
    lower_arrival,
    upper_arrival,
    in_phase_null=None,
    reverse_null=None,
    group="mono",
):
    state = {
        "latest_by_target": {
            f"{group}:woofer": _driver_record(
                "woofer", lower_arrival, calibrated=calibrated, group=group
            ),
            f"{group}:tweeter": _driver_record(
                "tweeter", upper_arrival, calibrated=calibrated, group=group
            ),
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
    state = _state(
        calibrated=True,
        lower_arrival=0.0100,
        upper_arrival=0.0106,
        in_phase_null=2.0,
    )
    out = build_crossover_alignment_proposal(
        _two_way(), state, requested_mode=ca.PHASE_AWARE
    )
    assert out["status"] == "ok"
    assert out["mode"]["mode"] == ca.PHASE_AWARE
    assert out["proposal"]["authorized"] is True
    assert out["proposal"]["delay_target_role"] == "woofer"


def test_build_proposal_refuses_phase_aware_on_uncalibrated_records():
    # The data-layer gate: even when phase_aware is REQUESTED, uncalibrated captures
    # downgrade it — a phone can never yield a phase/delay decision.
    state = _state(
        calibrated=False,
        lower_arrival=0.0100,
        upper_arrival=0.0106,
        in_phase_null=2.0,
    )
    out = build_crossover_alignment_proposal(
        _two_way(), state, requested_mode=ca.PHASE_AWARE
    )
    assert out["mode"]["mode"] == ca.MAGNITUDE_ONLY
    assert out["mode"]["downgraded"] is True
    assert out["proposal"]["authorized"] is False
    assert out["proposal"]["delay_ms"] is None


def test_build_proposal_reads_reverse_null_from_state():
    state = _state(
        calibrated=True,
        lower_arrival=0.010,
        upper_arrival=0.010,
        reverse_null=28.0,
    )
    out = build_crossover_alignment_proposal(
        _two_way(), state, requested_mode=ca.PHASE_AWARE
    )
    proposal = out["proposal"]
    assert proposal["reverse_null_depth_db"] == 28.0
    assert proposal["reverse_verdict"] == ca.REVERSE_STRONG
    assert proposal["polarity_action"] == ca.POLARITY_KEEP


def test_build_proposal_graceful_on_empty_state():
    out = build_crossover_alignment_proposal(
        _two_way(), {"latest_by_target": {}, "latest_summed_by_group": {}},
        requested_mode=ca.PHASE_AWARE,
    )
    assert out["status"] == "no_measurements"
    assert out["proposal"] is None
