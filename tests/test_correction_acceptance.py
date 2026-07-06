# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic verify-acceptance verdict (revision plan §4 P4).

These tests run the REAL :func:`jasper.correction.acceptance.evaluate_acceptance`
against synthetic curves with **known ground truth** — the P7 lesson: no I/O
stubbing of the thing under test, only synthetic inputs whose correct verdict is
known by construction. The four load-bearing scenarios (plan §8):

  * a genuinely-improved room  → ``accept``
  * a genuinely-regressed band → ``revert_pending_confirm`` (then ``revert`` on
    a concordant re-measure)
  * pure noise at the repeatability floor → ``surface`` (never revert on noise)
  * the ambiguous middle (a wash) → ``surface``

plus the statistical guards the naive rule (§8) would fail: 1/3-octave
aggregation before any per-band verdict, the "both criteria" clear-regression
rule, the confirmatory-re-measure concordance gate, and the env-tunable
thresholds' out-of-range fallback.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from jasper.correction import acceptance
from jasper.correction.acceptance import (
    AcceptanceThresholds,
    Verdict,
    evaluate_acceptance,
)


def _log_freqs(n: int = 480) -> np.ndarray:
    """480 log-spaced points 20 Hz - 20 kHz, matching the session pipeline's
    analysis.resample_log grid."""
    return np.geomspace(20.0, 20000.0, n)


def _bell(freqs: np.ndarray, fc: float, q: float, gain_db: float) -> np.ndarray:
    """Synthetic RBJ-peaking bell, same half-width model the pipeline uses,
    so a constructed 'room mode' is realistic."""
    if fc <= 0:
        return np.zeros_like(freqs)
    omega = freqs / fc
    safe = np.where(omega > 0, omega, 1.0)
    delta_oct = np.log2(safe)
    bw = math.asinh(1.0 / (2.0 * max(q, 1e-3))) / math.log(2.0)
    resp = gain_db / (1.0 + (delta_oct / bw) ** 2)
    resp[omega <= 0] = 0.0
    return resp


# A flat target (correction removes deviation *from* it). The band the verdict
# judges is [50, 350] Hz by default.
def _flat_target(freqs: np.ndarray) -> np.ndarray:
    return np.zeros_like(freqs)


# --------------------------------------------------------------------------
# Ground-truth scenario 1: a genuinely-improved room -> accept
# --------------------------------------------------------------------------


def test_genuinely_improved_room_is_accepted():
    """A +8 dB 60 Hz room mode, fully corrected at the verify. Error to target
    drops sharply and no band regresses -> accept."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    verify = np.zeros_like(f)  # perfectly flat after correction

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    assert r.verdict is Verdict.ACCEPT
    assert r.overall_rms_delta_db > 0.5  # improved beyond the floor
    assert r.regressed_band_count == 0
    assert not r.clear_regression


def test_partial_improvement_still_accepts_when_no_band_regresses():
    """A modal cut that halves the bump (not perfect) still accepts — the whole
    point is 'measurably better', not 'perfect'."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 55.0, 4.0, 9.0)
    verify = _bell(f, 55.0, 4.0, 4.0)  # residual half-bump

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    assert r.verdict is Verdict.ACCEPT
    assert r.regressed_band_count == 0


# --------------------------------------------------------------------------
# Ground-truth scenario 2: a genuinely-regressed band -> revert (pending/confirmed)
# --------------------------------------------------------------------------


def test_genuinely_regressed_band_pends_confirmation_on_first_verify():
    """The verify makes a NEW ~12 dB peak the before didn't have AND worsens
    the overall RMS. On the FIRST verify this is revert_pending_confirm — a
    clear regression, but not yet auto-reverted (one more sweep to confirm)."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    verify = before + _bell(f, 120.0, 3.0, 12.0)  # added damage at 120 Hz

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    assert r.verdict is Verdict.REVERT_PENDING_CONFIRM
    assert r.clear_regression
    assert not r.confirmed
    assert r.regressed_band_count >= 1
    assert r.overall_rms_delta_db < 0  # overall got worse
    # The worst band is near the added 120 Hz damage.
    assert r.worst_band_center_hz is not None
    assert 90.0 <= r.worst_band_center_hz <= 160.0


def test_confirmed_regression_reverts():
    """A clear regression that is CONCORDANT with a prior clear regression
    (second verify) escalates to revert — the auto-rollback trigger."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    verify = before + _bell(f, 120.0, 3.0, 12.0)

    r = evaluate_acceptance(
        freqs=f, before_db=before, verify_db=verify, target_db=target,
        verify_index=2, prior_clear_regression=True,
    )

    assert r.verdict is Verdict.REVERT
    assert r.confirmed
    assert r.clear_regression


def test_second_verify_that_is_now_fine_does_not_revert():
    """The concordance gate protects against a one-off bad sweep: if the first
    verify regressed but the SECOND (confirmatory) measures fine, we do NOT
    revert — the first was noise. This is the whole reason for the gate."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    verify_good = np.zeros_like(f)  # confirmatory sweep is clean

    r = evaluate_acceptance(
        freqs=f, before_db=before, verify_db=verify_good, target_db=target,
        verify_index=2, prior_clear_regression=True,
    )

    # A genuinely-improved second measurement accepts, never reverts, even
    # though a prior verify had flagged a regression.
    assert r.verdict is Verdict.ACCEPT
    assert not r.clear_regression


# --------------------------------------------------------------------------
# Ground-truth scenario 3: pure noise at the repeatability floor -> surface
# --------------------------------------------------------------------------


def test_pure_noise_at_repeatability_floor_surfaces_never_reverts():
    """The naive rule's failure mode (§8): the verify differs from the before
    only by seat-to-seat measurement noise INSIDE the 4-6 dB repeatability
    floor. It must NOT revert — no band clears the floor, and the overall RMS
    change is within the noise margin -> surface."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    rng = np.random.default_rng(7)
    # 2 dB std noise — well inside the 4-6 dB seat-to-seat floor.
    verify = before + rng.normal(0.0, 2.0, f.size)

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    assert r.verdict is Verdict.SURFACE
    assert not r.clear_regression
    assert r.regressed_band_count == 0


def test_white_noise_never_flags_across_many_seeds():
    """Mechanics sweep (per-point WHITE noise): 1/3-octave smoothing averages
    per-bin noise away, so sub-floor white noise never even pends. This pins
    the smoothing + AND-gate machinery; the FLOOR-level claim needs the
    spectrally-smooth model below, which smoothing cannot average away."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 70.0, 3.0, 7.0)
    for seed in range(40):
        rng = np.random.default_rng(seed)
        verify = before + rng.normal(0.0, 2.5, f.size)  # inside the floor
        r = evaluate_acceptance(
            freqs=f, before_db=before, verify_db=verify, target_db=target,
        )
        assert r.verdict in (Verdict.ACCEPT, Verdict.SURFACE), (seed, r.verdict)
        assert not r.clear_regression, seed


def _smooth_seat_noise(
    f: np.ndarray, std_db: float, rng: np.random.Generator,
) -> np.ndarray:
    """Spectrally-SMOOTH noise: N(0, std) at 1/3-octave knots, log-f
    interpolated. This is the shape spatial.py's 4-6 dB seat-to-seat std
    constants describe — broad curve differences that 1/3-octave smoothing
    does NOT average away (unlike per-point white noise), so it exercises the
    thresholds at the seeded floor for real."""
    n_knots = int(np.ceil(np.log2(20000.0 / 20.0) * 3)) + 1
    knots = np.geomspace(20.0, 20000.0, n_knots)
    vals = rng.normal(0.0, std_db, knots.size)
    return np.interp(np.log(f), np.log(knots), vals)


def test_smooth_noise_at_seeded_floors_never_terminal_reverts():
    """FLOOR-level safety claim, pinned (repo rule: pin promises with tests):
    a verify that differs from the before only by spectrally-smooth
    seat-to-seat noise AT the seeded floor constants (4 and 6 dB std — the
    spatial.py values the thresholds are seeded from) can pend a confirmation
    but can NEVER terminal-revert on a single sweep.

    Measured verdict split over these exact 300 fixed seeds (the H1 retuning
    target — on-device SAME-SEAT repeatability is expected to be far tighter
    than these seat-to-seat stds, and H1's measured numbers replace the
    placeholder thresholds):
      std=4 dB: 46/300 pend (15.3%), 248 surface, 6 accept, 0 revert
      std=6 dB: 184/300 pend (61.3%), 116 surface, 0 accept, 0 revert
    The loose bounds below allow small numeric drift without letting the
    pend rate silently explode (or the sweep silently stop pending, which
    would mean the thresholds no longer bind at the floor)."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    for std_db, pend_lo, pend_hi in ((4.0, 0.02, 0.30), (6.0, 0.30, 0.80)):
        pend = 0
        n = 300
        for seed in range(n):
            rng = np.random.default_rng(seed)
            verify = before + _smooth_seat_noise(f, std_db, rng)
            r = evaluate_acceptance(
                freqs=f, before_db=before, verify_db=verify, target_db=target,
            )
            # The hard safety line: a single sweep NEVER terminal-reverts.
            assert r.verdict is not Verdict.REVERT, (std_db, seed)
            if r.verdict is Verdict.REVERT_PENDING_CONFIRM:
                pend += 1
        rate = pend / n
        assert pend_lo <= rate <= pend_hi, (std_db, rate)


# --------------------------------------------------------------------------
# Ground-truth scenario 4: the ambiguous middle (a wash) -> surface
# --------------------------------------------------------------------------


def test_wash_is_surfaced_not_claimed_as_a_win():
    """The verify is essentially the before (a tiny uniform offset). No real
    improvement, no regression -> surface, never a claimed accept."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    verify = before + 0.2  # a hair different, within the noise floor

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    assert r.verdict is Verdict.SURFACE
    assert abs(r.overall_rms_delta_db) < 0.5


def test_local_trade_without_overall_regression_surfaces_never_reverts():
    """One band clears the per-band regression floor, but the overall RMS did
    NOT worsen (the correction traded a small local worsening for a big win
    elsewhere). This is the 'both criteria' guard: a single bad band alone
    never reverts — it surfaces."""
    f = _log_freqs()
    target = _flat_target(f)
    # Before: a big broad problem across the band.
    before = _bell(f, 80.0, 0.7, 14.0)
    # Verify: the broad problem is largely fixed, but a narrow bump was
    # introduced at 200 Hz that DOES clear the per-band regression floor.
    # Overall RMS still improves a lot, so it is not a *clear* regression.
    verify = _bell(f, 80.0, 0.7, 2.0) + _bell(f, 200.0, 2.0, 12.0)

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    assert r.overall_rms_delta_db > 0  # overall improved
    assert r.regressed_band_count >= 1  # a band genuinely cleared the floor
    # ...but overall did not worsen, so the 'both criteria' rule blocks revert.
    assert r.verdict is Verdict.SURFACE
    assert not r.clear_regression


# --------------------------------------------------------------------------
# The statistical guards (the naive-rule failure the plan §8 killed)
# --------------------------------------------------------------------------


def test_single_bin_spike_below_band_resolution_does_not_trip_a_verdict():
    """A one-bin spike (narrower than a 1/3-octave band) must be smoothed away
    by the aggregation before any per-band verdict — the plan's '>=1/3-octave
    aggregation, never raw per-bin' rule. A raw per-bin comparison would call
    this a regression; the aggregated verdict must not."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    verify = np.zeros_like(f)  # corrected flat...
    # ...except one lone bin near 150 Hz spikes +15 dB (a single-bin artifact).
    idx = int(np.argmin(np.abs(f - 150.0)))
    verify[idx] += 15.0

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    # The overall room still improved massively; the single-bin spike is
    # smoothed below the band regression floor and does not trip a revert.
    assert r.verdict is Verdict.ACCEPT
    assert r.regressed_band_count == 0


def test_bands_are_at_least_third_octave_wide():
    """The per-band table must be aggregated at >=1/3-octave (the plan's
    floor). Adjacent band centers should be roughly a factor of 2^(1/3) apart,
    never per-bin."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=before, target_db=target)

    centers = [b.center_hz for b in r.bands]
    assert len(centers) >= 2
    ratios = [centers[i + 1] / centers[i] for i in range(len(centers) - 1)]
    third_octave = 2.0 ** (1.0 / 3.0)
    # Every step is at least ~1/3-octave (allow a hair of float slack).
    assert min(ratios) >= third_octave - 1e-6


def test_clear_regression_requires_both_band_and_overall_criteria():
    """A whole-curve RMS wobble in the wrong direction, but with NO band
    clearing the per-band floor, is not a clear regression — the 'both
    criteria' rule. Construct a diffuse, sub-floor worsening across many bands
    that pushes overall RMS negative without any single band clearing 6 dB."""
    f = _log_freqs()
    target = _flat_target(f)
    before = np.zeros_like(f)  # started flat
    rng = np.random.default_rng(3)
    # A diffuse ~3 dB-std worsening: every band a little worse, none by >6 dB.
    verify = before + np.abs(rng.normal(0.0, 3.0, f.size))

    r = evaluate_acceptance(freqs=f, before_db=before, verify_db=verify, target_db=target)

    # The construction must genuinely hit the intended corner — assert it
    # outright (a conditional here would let the fixture drift into testing
    # nothing): overall RMS clearly worse than the 1.0 dB margin, yet NO band
    # cleared the 6 dB per-band floor...
    assert r.overall_rms_delta_db < -1.0
    assert r.regressed_band_count == 0
    # ...so the AND rule blocks the regression call: surface, not revert.
    assert r.verdict is Verdict.SURFACE
    assert not r.clear_regression


# --------------------------------------------------------------------------
# Matched-basis, verdict table, and serialization
# --------------------------------------------------------------------------


def test_basis_label_is_recorded():
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    r = evaluate_acceptance(
        freqs=f, before_db=before, verify_db=before, target_db=target,
        basis="position_1",
    )
    assert r.basis == "position_1"
    d = r.to_dict()
    assert d["basis"] == "position_1"
    assert d["verdict"] == r.verdict.value
    assert isinstance(d["bands"], list) and d["bands"]
    assert set(d["bands"][0]) == {
        "center_hz", "before_err_db", "after_err_db", "delta_db", "regressed",
    }
    # to_dict is JSON-serializable (no numpy scalars leak through).
    import json
    json.dumps(d)


def test_reasons_are_populated_for_every_verdict():
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    cases = [
        (np.zeros_like(f), 1, False),                       # accept
        (before + _bell(f, 120.0, 3.0, 12.0), 1, False),    # pending revert
        (before + _bell(f, 120.0, 3.0, 12.0), 2, True),     # confirmed revert
        (before + 0.2, 1, False),                           # wash / surface
    ]
    for verify, idx, prior in cases:
        r = evaluate_acceptance(
            freqs=f, before_db=before, verify_db=verify, target_db=target,
            verify_index=idx, prior_clear_regression=prior,
        )
        assert r.reasons, r.verdict
        assert all(isinstance(x, str) and x for x in r.reasons)


# --------------------------------------------------------------------------
# Degraded inputs never accept or revert (fail-soft to surface)
# --------------------------------------------------------------------------


def test_length_mismatch_surfaces():
    f = _log_freqs()
    r = evaluate_acceptance(
        freqs=f,
        before_db=np.zeros(10),
        verify_db=np.zeros_like(f),
        target_db=_flat_target(f),
    )
    assert r.verdict is Verdict.SURFACE
    assert r.bands == ()


def test_empty_curves_surface():
    empty = np.asarray([], dtype=np.float64)
    r = evaluate_acceptance(
        freqs=empty, before_db=empty, verify_db=empty, target_db=empty,
    )
    assert r.verdict is Verdict.SURFACE


def test_non_finite_values_surface():
    f = _log_freqs()
    bad = _flat_target(f).copy()
    bad[5] = np.inf
    r = evaluate_acceptance(
        freqs=f, before_db=bad, verify_db=_flat_target(f), target_db=_flat_target(f),
    )
    assert r.verdict is Verdict.SURFACE


def test_no_points_in_band_surfaces():
    """A grid entirely above the correction band has no points to judge."""
    f = np.geomspace(1000.0, 20000.0, 200)  # all above 350 Hz
    r = evaluate_acceptance(
        freqs=f,
        before_db=np.ones_like(f) * 3.0,
        verify_db=np.zeros_like(f),
        target_db=np.zeros_like(f),
    )
    assert r.verdict is Verdict.SURFACE


# --------------------------------------------------------------------------
# Env-tunable thresholds (JASPER_ACCEPT_*) — out-of-range falls back
# --------------------------------------------------------------------------


def test_thresholds_defaults_are_seeded_from_the_repeatability_floor():
    t = AcceptanceThresholds()
    # Seeded from spatial.MEDIUM_CONFIDENCE_STD_DB (6.0).
    assert t.band_regression_db == 6.0
    assert t.smoothing_fraction == 3  # >=1/3-octave floor
    assert t.overall_rms_regression_db > 0
    assert 0.0 <= t.overall_rms_improvement_db < t.band_regression_db


def test_env_override_is_applied(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JASPER_ACCEPT_BAND_REGRESSION_DB", "9.0")
    monkeypatch.setenv("JASPER_ACCEPT_SMOOTHING_FRACTION", "6")
    t = AcceptanceThresholds()
    assert t.band_regression_db == 9.0
    assert t.smoothing_fraction == 6


def test_out_of_range_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JASPER_ACCEPT_BAND_REGRESSION_DB", "999")  # out of [0.5, 24]
    monkeypatch.setenv("JASPER_ACCEPT_SMOOTHING_FRACTION", "not-a-number")
    t = AcceptanceThresholds()
    assert t.band_regression_db == 6.0  # fell back
    assert t.smoothing_fraction == 3  # fell back


def test_incoherent_threshold_pair_falls_back_wholesale(
    monkeypatch: pytest.MonkeyPatch,
):
    """An improvement floor >= the regression floor is nonsensical (accept
    harder than revert); from_env falls back to the whole default set rather
    than shipping an incoherent pair."""
    monkeypatch.setenv("JASPER_ACCEPT_OVERALL_RMS_IMPROVEMENT_DB", "10.0")
    monkeypatch.setenv("JASPER_ACCEPT_BAND_REGRESSION_DB", "6.0")
    t = AcceptanceThresholds.from_env()
    assert t.overall_rms_improvement_db == 0.5
    assert t.band_regression_db == 6.0


def test_looser_threshold_makes_a_borderline_regression_surface():
    """The env knob genuinely moves the verdict: a borderline 7 dB band
    regression that reverts at the default 6 dB floor merely surfaces when the
    floor is raised to 9 dB — proving the threshold is load-bearing."""
    f = _log_freqs()
    target = _flat_target(f)
    before = _bell(f, 60.0, 3.0, 8.0)
    # After 1/3-octave smoothing this worsens a band by ~8.6 dB — above a 6 dB
    # floor, below a 9 dB one — and worsens the overall RMS, so at the default
    # floor it is a clear regression.
    verify = before + _bell(f, 120.0, 2.0, 10.0)

    strict = evaluate_acceptance(
        freqs=f, before_db=before, verify_db=verify, target_db=target,
        thresholds=AcceptanceThresholds(band_regression_db=6.0),
    )
    loose = evaluate_acceptance(
        freqs=f, before_db=before, verify_db=verify, target_db=target,
        thresholds=AcceptanceThresholds(band_regression_db=9.0),
    )
    assert strict.regressed_band_count >= 1
    assert strict.verdict is Verdict.REVERT_PENDING_CONFIRM
    assert loose.regressed_band_count == 0
    assert loose.verdict is not Verdict.REVERT_PENDING_CONFIRM


# --------------------------------------------------------------------------
# Module surface
# --------------------------------------------------------------------------


def test_verdict_enum_values_are_stable_strings():
    # These strings land in event logs, result.json, and the envelope — pin
    # them so a rename is a conscious schema change.
    assert Verdict.ACCEPT.value == "accept"
    assert Verdict.SURFACE.value == "surface"
    assert Verdict.REVERT_PENDING_CONFIRM.value == "revert_pending_confirm"
    assert Verdict.REVERT.value == "revert"


def test_evaluate_acceptance_is_exported():
    assert hasattr(acceptance, "evaluate_acceptance")
    assert hasattr(acceptance, "AcceptanceThresholds")
    assert hasattr(acceptance, "AcceptanceResult")
    assert hasattr(acceptance, "Verdict")
