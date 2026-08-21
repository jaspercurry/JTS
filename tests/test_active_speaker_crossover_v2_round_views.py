# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The four round-grading comparison views, ported from a laptop campaign
(issue #2769): frozen-reference grading, per-seat curves including the
VERIFY pose, session-to-session repeatability, and per-seat agreement.

Every fixture builds its ``cloud_verify.json`` ``spec`` block by calling the
REAL :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` on a
synthetic combined curve and persisting its own ``to_dict()`` — the same
shape a real banked round carries — rather than hand-typing a partial dict,
so a schema drift in :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`
fails this suite instead of silently going unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.active_speaker.crossover_v2.round_views import (
    RoundViewsError,
    agreement_table,
    frozen_reference_grade,
    load_banked_round,
    per_seat_curves,
    repeatability_spread,
    verify_pose_curve,
)
from jasper.active_speaker.flat_spec import evaluate_flat_spec
from jasper.audio_measurement import sweep
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.deconv import deconvolve, magnitude_response
from jasper.audio_measurement.gating import gate_impulse_response

#: A log-spaced curve grid spanning all three SPEC_BANDS rows
#: (250-2000 / 2000-8000 / 8000-16000 Hz) with plenty of bins in each.
GRID = np.geomspace(280.0, 16000.0, 90)
REFERENCE_DB = -20.0


def _flat_curve(*, offset_db: float = 0.0, ripple_db: float = 0.0) -> np.ndarray:
    """A curve at ``REFERENCE_DB + offset_db``, with optional deterministic
    ripple (a single +ripple_db bump at bin 10, -ripple_db at bin 40) so a
    test can tell a perfectly-flat golden case apart from a rippled one."""
    curve = np.full(GRID.shape, REFERENCE_DB + offset_db, dtype=float)
    if ripple_db:
        curve[10] += ripple_db
        curve[40] -= ripple_db
    return curve


def _spec_dict(
    combined_db: np.ndarray, *, smoothing_fraction: int = 12, trusted_floor_hz: float | None = None
) -> dict[str, Any]:
    """A real ``FlatSpecReport.to_dict()`` for ``combined_db`` on :data:`GRID`."""
    mask = np.zeros(GRID.shape, dtype=bool)
    report = evaluate_flat_spec(
        GRID, combined_db, mask,
        smoothing_fraction=smoothing_fraction, trusted_floor_hz=trusted_floor_hz,
    )
    return report.to_dict()


def _make_round_dir(
    tmp_path: Path,
    name: str,
    *,
    position_curves: dict[str, tuple[str, np.ndarray]],
    combined_db: np.ndarray | None = None,
    spec_smoothing_fraction: int = 12,
    positions_smoothing_fraction: int | None = None,
    trusted_floor_hz: float | None = None,
) -> Path:
    """One banked round directory, in the tree ``bank-crossover-round.sh``
    produces: ``<round-dir>/bundle/<session>/evidence/v1/artifacts/crossover_v2/<relay>/``.

    ``position_curves`` maps ``position_id -> (role, magnitude_db)``.
    ``spec_smoothing_fraction`` / ``positions_smoothing_fraction`` are
    separate on purpose (they default equal): a real banked round's
    combined-curve ``spec`` block and its per-position ``curve_grid`` block
    are not always smoothed at the same fraction, and B3's mismatched-
    fraction test needs to set them apart deliberately.
    """
    if positions_smoothing_fraction is None:
        positions_smoothing_fraction = spec_smoothing_fraction
    round_dir = tmp_path / name
    session_dir = round_dir / "bundle" / "sess1"
    relay_dir = session_dir / "evidence/v1/artifacts/crossover_v2" / "cap1"
    relay_dir.mkdir(parents=True)

    (session_dir / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": "sess1", "state": "closed", "started_at": 1.0,
        "placement": {"policy_id": "driver_same_distance_v1", "acknowledged": True},
        "fingerprints": {
            "topology_id": "default", "topology_fingerprint": "abc123",
            "output_assignments": [],
            "graph_fingerprint": None,
            "mic": {"calibration_id": "", "calibration_sha256": None},
            "build_sha": "deadbeef",
        },
    }))
    (relay_dir / "round_receipt.json").write_text(json.dumps({
        "kind": "jts_crossover_v2_round_receipt", "schema_version": 2, "round_id": "r1",
    }))
    if combined_db is None:
        # Power-mean the supplied positions when the caller doesn't care.
        stack = np.vstack([curve for _role, curve in position_curves.values()])
        combined_db = 10.0 * np.log10(np.mean(10.0 ** (stack / 10.0), axis=0))
    positions = [
        {
            "position_id": position_id, "index": index, "attempt": 1,
            "role": role, "take_id": "", "magnitude_db": curve.tolist(),
        }
        for index, (position_id, (role, curve)) in enumerate(position_curves.items(), start=2)
    ]
    cloud = {
        "kind": "jts_crossover_v2_cloud_evidence", "schema_version": 1,
        "trusted_floor_hz": trusted_floor_hz, "validity_floor_hz": None,
        "curve": {"freqs_hz": GRID.tolist(), "magnitude_db": combined_db.tolist()},
        "flatness": {"evaluable": True, "n_bins": len(GRID), "n_excluded": 0, "rms_db": 0.0},
        "spec": _spec_dict(
            combined_db, smoothing_fraction=spec_smoothing_fraction, trusted_floor_hz=trusted_floor_hz,
        ),
        "merged_excluded_bands_hz": [], "screen_excluded_bands_hz": [],
        "null_registry": {"classification": "insufficient_evidence", "nulls": []},
        "null_registry_crossover_region": {"classification": "insufficient_evidence"},
        "carve_outs": [], "geometry": {"reason": "thin_evidence", "n_positions": len(positions)},
        "positions": {
            "available": True, "schema": "jts_attribution_position_evidence/1",
            "curve_grid": {
                "freqs_hz": GRID.tolist(), "fractional_octave": positions_smoothing_fraction,
                "smoothing_fraction": positions_smoothing_fraction, "floor_hz": None, "floor_source": None,
            },
            "positions": positions,
        },
    }
    (relay_dir / "cloud_verify.json").write_text(json.dumps(cloud))
    (relay_dir / "findings_cloud_verify.json").write_text(json.dumps({
        "findings": [], "field_descriptions": {},
    }))
    return round_dir


# --------------------------------------------------------------------------- #
# load_banked_round
# --------------------------------------------------------------------------- #


def test_load_banked_round_reads_positions_and_grid(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve()),
            "cloud_verify_04": ("offax", _flat_curve()),
        },
    )
    banked = load_banked_round(round_dir)
    assert {p.position_id for p in banked.positions} == {"cloud_verify_02", "cloud_verify_04"}
    assert banked.curve_grid_hz.shape == GRID.shape
    assert banked.report.bands  # a real, non-empty rehydrated report


def test_load_banked_round_refuses_missing_bundle(tmp_path):
    with pytest.raises(RoundViewsError, match="bundle"):
        load_banked_round(tmp_path / "nope")


def test_load_banked_round_refuses_multiple_bundle_sessions(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    (round_dir / "bundle" / "second_session").mkdir()
    with pytest.raises(RoundViewsError, match="expected exactly one"):
        load_banked_round(round_dir)


# --------------------------------------------------------------------------- #
# View 1 — frozen_reference_grade
# --------------------------------------------------------------------------- #


def test_frozen_equals_shipped_when_target_is_the_baseline(tmp_path):
    """Grading a round against itself: the freeze changes nothing."""
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve(ripple_db=1.0)),
            "cloud_verify_04": ("offax", _flat_curve(ripple_db=0.5)),
        },
    )
    banked = load_banked_round(round_dir)
    result = frozen_reference_grade(banked, banked)
    assert result.shipped == pytest.approx(result.frozen, abs=1e-9)


def test_frozen_reference_recovers_an_injected_level_shift_exactly(tmp_path):
    """The golden case the module docstring derives: a perfectly FLAT
    baseline and a perfectly flat target shifted by ``delta_db`` grade
    SHIPPED as zero-residual either way (a pure level shift is invisible to
    a curve's own reference — power-mean is exactly additive under a
    per-bin constant dB shift), but FROZEN grades the target against the
    baseline's un-shifted reference, so every bin's frozen deviation is
    exactly ``delta_db`` and the pooled RMS is exactly ``abs(delta_db)``.
    """
    delta_db = 0.6
    baseline_dir = _make_round_dir(
        tmp_path, "baseline",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    target_dir = _make_round_dir(
        tmp_path, "target",
        position_curves={"cloud_verify_02": ("onax", _flat_curve(offset_db=delta_db))},
    )
    baseline = load_banked_round(baseline_dir)
    target = load_banked_round(target_dir)
    result = frozen_reference_grade(baseline, target)

    assert result.shipped["onax"] == pytest.approx(0.0, abs=1e-9)
    assert result.frozen["onax"] == pytest.approx(abs(delta_db), abs=1e-6)
    assert result.shipped_positions["cloud_verify_02"] == pytest.approx(0.0, abs=1e-9)
    assert result.frozen_positions["cloud_verify_02"] == pytest.approx(abs(delta_db), abs=1e-6)


def test_frozen_reference_grader_matches_evaluate_flat_spec_directly(tmp_path):
    """The SHIPPED half is not a re-derivation: it must equal what calling
    the real evaluator directly on the same curve produces."""
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=0.8))},
    )
    banked = load_banked_round(round_dir)
    result = frozen_reference_grade(banked, banked)

    direct = evaluate_flat_spec(
        GRID, _flat_curve(ripple_db=0.8), np.zeros(GRID.shape, dtype=bool),
        smoothing_fraction=12, trusted_floor_hz=None,
    )
    from jasper.active_speaker.flat_spec import spec_convergence_residual
    expected = spec_convergence_residual(direct)
    assert result.shipped["onax"] == pytest.approx(expected.rms_db, abs=1e-9)


def test_frozen_reference_refuses_a_target_position_absent_from_baseline(tmp_path):
    baseline_dir = _make_round_dir(
        tmp_path, "baseline", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    target_dir = _make_round_dir(
        tmp_path, "target", position_curves={"cloud_verify_09": ("onax", _flat_curve())},
    )
    baseline = load_banked_round(baseline_dir)
    target = load_banked_round(target_dir)
    with pytest.raises(RoundViewsError, match="no baseline counterpart"):
        frozen_reference_grade(baseline, target)


# --------------------------------------------------------------------------- #
# View 2 — verify_pose_curve + per_seat_curves
# --------------------------------------------------------------------------- #


def _write_wired_capture(
    round_dir: Path,
    session_dir: Path,
    *,
    phase: str = "verify",
    n_takes: int = 1,
    reflection_delay_samples: int | None = None,
    reflection_gain: float = 0.0,
    noise_scale: float = 1e-5,
    noise_seed: int = 0,
    bank_shape: bool = False,
) -> np.ndarray:
    """Bank a ``{phase}_program.wav`` plus ``n_takes`` dump-ring captures of
    it through a synthetic "room" — a pure delay by default (an identity
    system beyond a fixed propagation delay, the same synthetic-IR shape
    ``tests/test_correction_sweep_deconv.py`` uses for the underlying DSP),
    plus one optional reflection.

    ``bank_shape=True`` banks the program WAV the way the product's own sole
    producer (``_play`` in ``jasper.web.correction_crossover_v2``) always
    does: in a SIBLING ``crossover_v2/<relay>/`` directory next to — not
    inside — ``evidence/``. Every real banked round is in this shape; the
    default (``False``) is the shape this suite's fixtures assumed instead,
    which a #2796 gate review found the product has never once produced.

    Returns the truth IR's peak sample offset, for the caller's own checks.
    """
    sig, meta = sweep.synchronized_swept_sine(duration_approx_s=1.0, sample_rate=48000)
    sr = meta.sample_rate
    relay_dir = next((session_dir / "evidence/v1/artifacts/crossover_v2").iterdir())
    program_dir = (session_dir / "crossover_v2" / relay_dir.name) if bank_shape else relay_dir
    program_dir.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(program_dir / f"{phase}_program.wav", sig, sr)

    delay_samples = 480  # 10 ms
    length = delay_samples + 50
    if reflection_delay_samples is not None:
        length = max(length, delay_samples + reflection_delay_samples + 50)
    ir_truth = np.zeros(length, dtype=np.float64)
    ir_truth[delay_samples] = 1.0
    if reflection_delay_samples is not None:
        ir_truth[delay_samples + reflection_delay_samples] = reflection_gain
    rng = np.random.default_rng(noise_seed)

    wav_dir = round_dir / "dumps" / "wav"
    sidecar_dir = round_dir / "dumps" / "sidecar"
    wav_dir.mkdir(parents=True)
    sidecar_dir.mkdir(parents=True)
    for i in range(n_takes):
        captured = fftconvolve(sig.astype(np.float64), ir_truth, mode="full")
        # Noise so the post-arrival span is not exact digital silence (the
        # reflection-envelope detector expects a real floor); amplified by
        # ``noise_scale`` past the default trace where a test needs the
        # raw, un-smoothed magnitude response to carry measurable ripple.
        captured = captured + rng.normal(scale=noise_scale, size=captured.shape)
        basename = f"{1000 + i}_{phase}_synthetic"
        sweep.write_sweep_wav(wav_dir / f"{basename}.wav", captured.astype(np.float32), sr)
        (sidecar_dir / f"{basename}.json").write_text(json.dumps({"phase": phase}))
    return ir_truth


def test_verify_pose_curve_absent_without_dump_ring(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    result = verify_pose_curve(banked)
    assert result.curve is None
    assert result.n_captures == 0
    assert result.reason


def test_verify_pose_curve_recovers_a_flat_response_from_an_identity_capture(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    _write_wired_capture(round_dir, banked.session_dir, n_takes=2)

    result = verify_pose_curve(banked)
    assert result.curve is not None
    assert result.n_captures == 2
    assert result.curve.position_id == "verify"
    assert result.curve.role == "verify"
    assert result.curve.freqs_hz.shape == banked.curve_grid_hz.shape

    # A pure-delay "room" is an all-pass system: the recovered magnitude
    # response should be flat well within the trusted sweep, once each
    # curve is normalised against its own median level the way
    # per_seat_curves compares it to the cloud positions.
    seats = per_seat_curves(banked, result.curve)
    verify_seat = next(s for s in seats if s.position_id == "verify")
    trusted = (banked.curve_grid_hz >= 500.0) & (banked.curve_grid_hz <= 12000.0)
    assert np.max(np.abs(verify_seat.normalized_db[trusted])) < 3.0


def test_verify_pose_curve_resolves_the_bank_shape_program_wav(tmp_path):
    """The shape every real banked round is actually in.

    A #2796 gate review proved the product's sole program-WAV producer
    (``_play`` in ``jasper.web.correction_crossover_v2``) has never once
    written into ``evidence/v1/artifacts/`` — a tree-wide sweep of a real
    banked round found zero ``*_program.wav`` files there. Before
    ``_find_program_wav`` adopted
    ``evidence_packet.round_program_dir``, this view returned "no
    verify_program.wav banked" on every real bundle, silently inert since
    #2769/#2781 shipped it. This is that failure, reproduced synthetically
    with the program WAV where the product actually banks it.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    _write_wired_capture(round_dir, banked.session_dir, n_takes=2, bank_shape=True)

    result = verify_pose_curve(banked)
    assert result.curve is not None
    assert result.n_captures == 2
    assert result.curve.position_id == "verify"


def test_verify_pose_curve_smooths_at_the_positions_fraction_not_the_spec_ones(tmp_path):
    """B3: a real banked round's combined-curve ``spec`` and per-position
    ``curve_grid`` are not always smoothed at the same fraction (the cloud
    smooths member positions finer than the combined curve on a real
    corpus), AND a real round often carries a non-``None`` trusted floor.
    Both are exercised here at once, matching real-round shape. The VERIFY
    pose must smooth at the POSITIONS' fraction (6, coarser) — using the
    spec's (3, finer) would leave more raw FFT ripple in the result, an
    artifact of which report field this function happened to read rather
    than of anything the speaker did.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
        spec_smoothing_fraction=3, positions_smoothing_fraction=6, trusted_floor_hz=180.0,
    )
    banked = load_banked_round(round_dir)
    assert banked.report.smoothing_fraction == 3
    assert banked.positions[0].smoothing_fraction == 6
    assert banked.report.trusted_floor_hz == 180.0
    _write_wired_capture(round_dir, banked.session_dir, n_takes=1)

    result = verify_pose_curve(banked)
    assert result.curve is not None
    assert result.curve.smoothing_fraction == 6, (
        "verify_pose_curve must default to the POSITIONS' smoothing fraction, "
        "not the report's (spec's)"
    )


def test_verify_pose_curve_gating_removes_a_reflection_comb_artifact(tmp_path):
    """S6 mutation-killer: a synthetic "room" with a real reflection (0.5
    gain, 3 ms after the direct arrival — squarely inside
    ``gating.SEARCH_T_MIN_MS``..``SEARCH_T_MAX_MS``) produces a comb-
    filtered magnitude response if left ungated. ``verify_pose_curve``'s
    real (gated) result stays flat within a tolerance the manually
    reconstructed UNGATED curve — built from the SAME captured bytes via
    the same ``deconvolve``/``magnitude_response``/``smooth_fractional_octave``
    calls, just skipping ``gate_impulse_response`` — measurably exceeds.
    Deleting the gate call inside ``verify_pose_curve`` would make its
    output equal the ungated curve computed here, which fails this bound.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    _write_wired_capture(
        round_dir, banked.session_dir, n_takes=1,
        reflection_delay_samples=144, reflection_gain=0.5,  # 3 ms, -6 dB
    )

    result = verify_pose_curve(banked)
    assert result.curve is not None
    seats = per_seat_curves(banked, result.curve)
    verify_seat = next(s for s in seats if s.position_id == "verify")
    trusted = (banked.curve_grid_hz >= 500.0) & (banked.curve_grid_hz <= 12000.0)
    gated_max_dev = float(np.max(np.abs(verify_seat.normalized_db[trusted])))

    # The same bytes, the same deconvolution, the same smoothing — the ONLY
    # difference is that gating is skipped.
    relay_dir = next(banked.session_dir.glob("evidence/v1/artifacts/crossover_v2/*"))
    program, program_sr = sweep.read_wav_mono(relay_dir / "verify_program.wav")
    captured, sr = sweep.read_wav_mono(next((round_dir / "dumps" / "wav").glob("*.wav")))
    assert sr == program_sr
    ir = deconvolve(captured, program, sr)
    freqs_lin, mag_db_lin = magnitude_response(ir, sr)  # NOT gated
    mag_smoothed = smooth_fractional_octave(freqs_lin, mag_db_lin, fraction=result.curve.smoothing_fraction)
    ungated_curve = np.interp(banked.curve_grid_hz, freqs_lin, mag_smoothed)
    sel = (banked.curve_grid_hz >= 400.0) & (banked.curve_grid_hz <= 8000.0)
    ungated_normalized = ungated_curve - float(np.median(ungated_curve[sel]))
    ungated_max_dev = float(np.max(np.abs(ungated_normalized[trusted])))

    tolerance = 4.0
    assert gated_max_dev < tolerance, gated_max_dev
    assert ungated_max_dev > tolerance, ungated_max_dev


def test_verify_pose_curve_smoothing_reduces_raw_fft_roughness(tmp_path):
    """S6 mutation-killer: amplified capture noise (still a fixed seed —
    fully deterministic) leaves measurable ripple in the RAW magnitude
    response; ``smooth_fractional_octave`` must reduce it. Compares the
    real ``verify_pose_curve`` result's roughness (sum of squared bin-to-
    bin differences over the trusted band) against the SAME bytes run
    through the identical chain with the smoothing step skipped. Deleting
    the ``smooth_fractional_octave`` call inside ``verify_pose_curve``
    would make its output equal the unsmoothed curve computed here, which
    fails the ratio bound.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
        positions_smoothing_fraction=3,
    )
    banked = load_banked_round(round_dir)
    assert banked.positions[0].smoothing_fraction == 3
    _write_wired_capture(round_dir, banked.session_dir, n_takes=1, noise_scale=0.02)

    result = verify_pose_curve(banked)
    assert result.curve is not None
    seats = per_seat_curves(banked, result.curve)
    verify_seat = next(s for s in seats if s.position_id == "verify")
    trusted = (banked.curve_grid_hz >= 500.0) & (banked.curve_grid_hz <= 12000.0)
    smoothed_roughness = float(np.sum(np.diff(verify_seat.normalized_db[trusted]) ** 2))

    relay_dir = next(banked.session_dir.glob("evidence/v1/artifacts/crossover_v2/*"))
    program, program_sr = sweep.read_wav_mono(relay_dir / "verify_program.wav")
    captured, sr = sweep.read_wav_mono(next((round_dir / "dumps" / "wav").glob("*.wav")))
    assert sr == program_sr
    ir = deconvolve(captured, program, sr)
    gated_ir, _fragment = gate_impulse_response(ir, sr)
    freqs_lin, mag_db_lin = magnitude_response(gated_ir, sr)  # NOT smoothed
    unsmoothed_curve = np.interp(banked.curve_grid_hz, freqs_lin, mag_db_lin)
    sel = (banked.curve_grid_hz >= 400.0) & (banked.curve_grid_hz <= 8000.0)
    unsmoothed_normalized = unsmoothed_curve - float(np.median(unsmoothed_curve[sel]))
    unsmoothed_roughness = float(np.sum(np.diff(unsmoothed_normalized[trusted]) ** 2))

    assert unsmoothed_roughness > 1.5 * smoothed_roughness, (smoothed_roughness, unsmoothed_roughness)


def test_per_seat_curves_includes_every_position_and_the_verify_pose(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve(offset_db=2.0)),
            "cloud_verify_04": ("offax", _flat_curve(offset_db=-3.0)),
        },
    )
    banked = load_banked_round(round_dir)
    _write_wired_capture(round_dir, banked.session_dir)
    verify = verify_pose_curve(banked)

    seats = per_seat_curves(banked, verify.curve)
    assert {s.position_id for s in seats} == {"cloud_verify_02", "cloud_verify_04", "verify"}
    # Every seat is self-normalised: a constant offset added before
    # normalisation must vanish from its own normalized curve.
    for seat in seats:
        if seat.position_id == "cloud_verify_02":
            assert np.allclose(seat.normalized_db, 0.0, atol=1e-9)


def test_per_seat_curves_refuses_an_empty_norm_band(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    with pytest.raises(RoundViewsError, match="norm band"):
        per_seat_curves(banked, None, norm_band_hz=(50000.0, 60000.0))


def test_per_seat_curves_normalises_by_median_not_mean(tmp_path):
    """Pins the MEDIAN normalisation specifically: a single huge outlier
    bin inside the norm band pulls the MEAN of that band well away from
    the baseline level, but the median (66 bins, one outlier) is untouched.
    A mutation that swapped ``np.median`` for ``np.mean`` would shift every
    baseline bin's ``normalized_db`` off zero by the outlier's pull —
    ~1.06 dB here, far outside float tolerance.
    """
    curve = _flat_curve()
    # GRID spans [280, 16000] Hz log-spaced; the default norm band is
    # [400, 8000] Hz. Push ONE bin inside that band to a large positive
    # outlier, leaving the rest of the band (and the whole curve) untouched.
    sel = (GRID >= 400.0) & (GRID <= 8000.0)
    outlier_idx = int(np.where(sel)[0][0])
    curve[outlier_idx] = 50.0
    assert np.median(curve[sel]) == pytest.approx(REFERENCE_DB)
    assert np.mean(curve[sel]) != pytest.approx(REFERENCE_DB, abs=0.5)

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", curve)},
    )
    banked = load_banked_round(round_dir)
    seats = per_seat_curves(banked, None)
    seat = seats[0]
    # A baseline bin OUTSIDE the outlier and inside the norm band must
    # normalise to exactly zero under median normalisation.
    baseline_idx = int(np.where(sel)[0][1])
    assert seat.normalized_db[baseline_idx] == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# View 3 — repeatability_spread
# --------------------------------------------------------------------------- #


def test_repeatability_spread_reports_zero_for_identical_repeats(tmp_path):
    curves = {"cloud_verify_02": ("onax", _flat_curve(ripple_db=0.7))}
    r1 = load_banked_round(_make_round_dir(tmp_path, "r1", position_curves=curves))
    r2 = load_banked_round(_make_round_dir(tmp_path, "r2", position_curves=curves))
    result = repeatability_spread([("session-1", r1), ("session-2", r2)])

    shipped = next(m for m in result.metrics if m.name == "shipped_linear_pool_db")
    assert shipped.values["session-1"] == pytest.approx(shipped.values["session-2"], abs=1e-9)
    spread = shipped.spread()
    assert spread is not None
    assert spread["range"] == pytest.approx(0.0, abs=1e-9)
    assert spread["sd"] == pytest.approx(0.0, abs=1e-9)

    position = next(m for m in result.per_position if m.name == "cloud_verify_02")
    assert position.values["session-1"] == pytest.approx(position.values["session-2"], abs=1e-9)


def test_repeatability_spread_pairwise_math_on_a_known_delta(tmp_path):
    """Two sessions whose SHIPPED number differs by a known, hand-derived
    amount (own-reference grading is invariant to a pure shift, so the
    delta is injected as ripple, not offset — see the frozen-reference
    golden test for the offset-invariance derivation)."""
    r1 = load_banked_round(
        _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    )
    r2 = load_banked_round(
        _make_round_dir(
            tmp_path, "r2",
            position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=1.2))},
        )
    )
    result = repeatability_spread([("a", r1), ("b", r2)])
    shipped = next(m for m in result.metrics if m.name == "shipped_linear_pool_db")
    assert shipped.values["a"] == pytest.approx(0.0, abs=1e-9)
    assert shipped.values["b"] > shipped.values["a"]
    spread = shipped.spread()
    assert spread["n"] == 2.0
    assert spread["range"] == pytest.approx(shipped.values["b"] - shipped.values["a"], abs=1e-9)
    assert spread["mean"] == pytest.approx((shipped.values["a"] + shipped.values["b"]) / 2.0, abs=1e-9)


def test_repeatability_spread_single_round_has_no_spread(tmp_path):
    r1 = load_banked_round(
        _make_round_dir(tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())})
    )
    result = repeatability_spread([("only", r1)])
    shipped = next(m for m in result.metrics if m.name == "shipped_linear_pool_db")
    assert shipped.spread() is None


def test_repeatability_metric_spread_uses_sample_variance_ddof1_not_population():
    """Pins the ``ddof=1`` (Bessel-corrected, SAMPLE) standard deviation,
    distinguished from the population (``ddof=0``) one on values chosen so
    the two read differently to more than rounding: for ``[1, 2, 3]``, the
    sample sd is exactly ``1.0`` and the population sd is
    ``sqrt(2/3) ≈ 0.8165`` — a mutation that dropped the ``-1`` (or changed
    ``len(vs) - 1`` to ``len(vs)``) would silently swap one for the other.
    """
    from jasper.active_speaker.crossover_v2.round_views import RepeatabilityMetric

    metric = RepeatabilityMetric("test", {"a": 1.0, "b": 2.0, "c": 3.0})
    spread = metric.spread()
    assert spread is not None
    assert spread["mean"] == pytest.approx(2.0)
    assert spread["sd"] == pytest.approx(1.0, abs=1e-9)
    # The population figure this must NOT equal, named so the distinction
    # is checkable rather than asserted from memory.
    population_sd = (sum((v - 2.0) ** 2 for v in (1.0, 2.0, 3.0)) / 3.0) ** 0.5
    assert spread["sd"] != pytest.approx(population_sd, abs=1e-9)


# --------------------------------------------------------------------------- #
# Agreement — testify/dissent classification
# --------------------------------------------------------------------------- #


def _seat_curve(position_id: str, role: str, values: np.ndarray):
    from jasper.active_speaker.crossover_v2.round_views import SeatCurve

    return SeatCurve(position_id=position_id, role=role, normalized_db=values)


def test_agreement_marks_a_feature_common_mode_when_every_seat_agrees():
    """A -6 dB dip at the same bin, same sign, same rough size, at every
    seat: sign agreement AND magnitude agreement -> COMMON-MODE.

    The dip is deep (6 dB, not 1 dB) because :func:`_detrend`'s 1-octave
    window includes the dip's own bins, so a narrow, shallow dip is mostly
    cancelled by its own local average before ``agreement_table`` ever sees
    it — the same shrinkage the campaign's own detrend has. 6 dB survives
    detrending with several dB of residual, well clear of ``testify_db``.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    base = np.zeros_like(grid)
    dip_idx = 25
    seats = []
    for i, name in enumerate(["s0", "s1", "s2", "s3"]):
        curve = base.copy()
        curve[dip_idx - 1 : dip_idx + 2] -= 6.0 + 0.2 * i  # near-identical depth
        seats.append(_seat_curve(name, "onax" if i % 2 == 0 else "offax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    assert features
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.pooled_db < 0
    assert feature.n_testify == 4
    assert feature.n_dissent == 0
    assert feature.common_mode is True


def test_agreement_reports_sign_ok_size_split_when_magnitude_disagrees():
    """Every seat dips the same direction (sign agreement holds), but one
    seat's dip is far deeper than the others (magnitude ratio > 3:1) —
    the campaign's own named failure mode (the 1400 Hz cut)."""
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    depths = [6.0, 6.0, 6.0, 30.0]  # last seat: 5x deeper
    seats = []
    for i, depth in enumerate(depths):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] -= depth
        seats.append(_seat_curve(f"s{i}", "onax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.n_dissent == 0
    assert feature.ratio > 3.0
    assert feature.common_mode is False


def test_agreement_reports_dissent_when_a_seat_disagrees_in_sign():
    """Three seats dip, one rises: the dissenting seat is counted and named,
    and — the campaign's own literal ``diss <= 1`` tolerance — a single
    dissenter among four testify=3 seats still leaves the feature
    COMMON-MODE (testify(3) >= AGREEMENT_TESTIFY_MIN(3), dissent(1) <=
    AGREEMENT_DISSENT_MAX(1)). What this test pins is the counting, not
    just the verdict: ``n_dissent`` must isolate exactly the one seat whose
    sign disagreed, never miscount it as a testifying seat."""
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    signs = [-1.0, -1.0, -1.0, +1.0]  # one seat goes the OTHER way
    seats = []
    for i, sign in enumerate(signs):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] += sign * 6.0
        seats.append(_seat_curve(f"s{i}", "onax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.n_testify == 3
    assert feature.n_dissent == 1
    assert feature.common_mode is True
    assert set(feature.seat_values_db) == {"s0", "s1", "s2", "s3"}


def test_agreement_n5_uses_the_literal_threshold_not_a_seat_count_generalisation():
    """B2's regression: at the REAL DEFAULT 5-seat cloud, a generalisation
    that scaled the testify requirement to ``len(seats) - 1`` (4 of 5) would
    refuse this feature; the campaign's own literal threshold (``testify >=
    3``) accepts it. 3 seats dip deep, 2 dip shallow but same-sign and
    within magnitude-agreement ratio of the deep three — sign agreement
    (testify=3, dissent=0) and magnitude agreement (ratio <= 3.0) both hold
    under the literal rule.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    depths = [6.0, 6.0, 6.0, 4.0, 4.0]
    seats = []
    for i, depth in enumerate(depths):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] -= depth
        seats.append(_seat_curve(f"s{i}", "onax", curve))

    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert len(seats) == 5
    assert feature.n_testify == 3  # < 5 - 1 = 4: a seat-count-relative rule would refuse this
    assert feature.n_dissent == 0
    assert feature.ratio <= 3.0
    assert feature.common_mode is True


def test_agreement_below_testify_min_seats_is_not_evaluable_never_a_vacuous_bool():
    """Below AGREEMENT_TESTIFY_MIN seats, ``testify >= 3`` cannot be
    satisfied by construction (there are not 3 seats to testify). The
    verdict is ``None`` — a named not-evaluable state — at both 1 and 2
    seats, never a fabricated ``True`` (the old ``n_seats - 1``
    generalisation floored at 0 and returned a vacuous pass there) and
    never a fabricated ``False`` either. The measurements themselves
    (``n_testify``, ``n_dissent``) are still reported — they are not
    verdicts, and stay informative even where the verdict cannot be.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    for n_seats in (1, 2):
        seats = []
        for i in range(n_seats):
            curve = np.zeros_like(grid)
            curve[dip_idx - 1 : dip_idx + 2] -= 6.0
            seats.append(_seat_curve(f"s{i}", "onax", curve))
        features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
        feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
        assert feature.n_testify == n_seats
        assert feature.common_mode is None, f"n_seats={n_seats}"


def test_agreement_dissent_above_max_fails_the_bar_even_with_testify_and_magnitude_ok():
    """Pins ``AGREEMENT_DISSENT_MAX`` specifically, isolated from the
    testify count and the magnitude ratio: 3 seats testify (meets
    ``AGREEMENT_TESTIFY_MIN``), 2 dissent (exceeds ``AGREEMENT_DISSENT_MAX
    == 1``), and the ratio is <= 3.0 (magnitude agreement holds). Only the
    dissent count can be failing the bar here — a mutation that widened
    ``AGREEMENT_DISSENT_MAX`` to 2 (or dropped the check) would flip this
    from ``False`` to ``True`` with nothing else changing.
    """
    grid = np.geomspace(400.0, 16000.0, 60)
    dip_idx = 25
    combo = [-6.0, -6.0, -6.0, 4.0, 4.0]
    seats = []
    for i, depth in enumerate(combo):
        curve = np.zeros_like(grid)
        curve[dip_idx - 1 : dip_idx + 2] += depth
        seats.append(_seat_curve(f"s{i}", "onax", curve))
    features = agreement_table(seats, grid, lo_hz=400.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    feature = min(features, key=lambda f: abs(f.center_hz - grid[dip_idx]))
    assert feature.n_testify == 3
    assert feature.n_dissent == 2
    assert feature.ratio <= 3.0
    assert feature.common_mode is False


def test_agreement_table_refuses_an_empty_seat_list():
    grid = np.geomspace(400.0, 16000.0, 60)
    with pytest.raises(RoundViewsError, match="no seats"):
        agreement_table([], grid, lo_hz=400.0, hi_hz=16000.0)


def test_agreement_respects_the_swept_band(tmp_path):
    """A feature outside [lo_hz, hi_hz] is not reported at all."""
    grid = np.geomspace(400.0, 16000.0, 60)
    curve = np.zeros_like(grid)
    curve[5] -= 2.0  # a feature near the very bottom of the grid
    seats = [_seat_curve("s0", "onax", curve)]
    features = agreement_table(seats, grid, lo_hz=2000.0, hi_hz=16000.0, feature_db=0.4, testify_db=0.4)
    assert all(f.center_hz >= 2000.0 for f in features)


# --------------------------------------------------------------------------- #
# CLI wiring — jasper-round-views
# --------------------------------------------------------------------------- #


def test_cli_frozen_writes_result_into_the_target_round_dir(tmp_path):
    from jasper.cli.round_views import main

    baseline_dir = _make_round_dir(
        tmp_path, "baseline", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    target_dir = _make_round_dir(
        tmp_path, "target",
        position_curves={"cloud_verify_02": ("onax", _flat_curve(offset_db=0.5))},
    )
    rc = main(["frozen", str(baseline_dir), str(target_dir)])
    assert rc == 0
    out_path = target_dir / "frozen_reference.json"
    assert out_path.is_file()
    payload = json.loads(out_path.read_text())
    assert payload["shipped"]["onax"] == pytest.approx(0.0, abs=1e-9)
    assert payload["frozen"]["onax"] == pytest.approx(0.5, abs=1e-6)


def test_cli_per_seat_writes_seats_including_absent_verify_reason(tmp_path):
    from jasper.cli.round_views import main

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    rc = main(["per-seat", str(round_dir)])
    assert rc == 0
    payload = json.loads((round_dir / "per_seat.json").read_text())
    assert [s["position_id"] for s in payload["seats"]] == ["cloud_verify_02"]
    assert payload["verify_pose"]["included"] is False
    assert payload["verify_pose"]["reason"]


def test_cli_reports_exit_1_on_an_unreadable_round(tmp_path, capsys):
    from jasper.cli.round_views import main

    rc = main(["per-seat", str(tmp_path / "nope")])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_cli_reports_exit_1_not_a_traceback_on_a_header_truncated_wav(tmp_path, capsys):
    """SF-2: a dump-ring WAV truncated inside its RIFF/fmt header raises
    ``struct.error`` from ``scipy.io.wavfile.read`` — NOT ``ValueError`` —
    which the CLI's exception tuple did not originally cover, so this shape
    reached the operator as an unhandled traceback instead of the
    documented exit 1. Verified by hand that a real WAV cut at 30 bytes
    raises exactly ``struct.error`` (10 bytes raises ``ValueError``
    instead, already covered; 44+ bytes is a DATA truncation that raises
    nothing at all, out of this tool's scope).
    """
    from jasper.cli.round_views import main

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    banked = load_banked_round(round_dir)
    _write_wired_capture(round_dir, banked.session_dir, n_takes=1)
    wav_path = next((round_dir / "dumps" / "wav").glob("*.wav"))
    data = wav_path.read_bytes()
    assert len(data) > 30
    wav_path.write_bytes(data[:30])  # header-truncated: struct.error on read

    rc = main(["per-seat", str(round_dir)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err
