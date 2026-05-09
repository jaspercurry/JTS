"""PEQ designer: greedy peak-fit on synthetic frequency responses.

The PEQ designer is the "make it audibly better" step. If it picks
filters that don't match the dominant peaks, the corrected room
sounds the same as the uncorrected one. These tests pin the
behavior on synthetic curves with known peak structure.
"""
from __future__ import annotations

import numpy as np

from jasper.correction import peq, target


def _log_freqs(n: int = 480) -> np.ndarray:
    """480 log-spaced points 20 Hz – 20 kHz, matching what the
    session.py pipeline produces from analysis.resample_log."""
    return np.geomspace(20.0, 20000.0, n)


def _bell(freqs: np.ndarray, fc: float, q: float, gain_db: float) -> np.ndarray:
    """Synthetic bell-shape used to construct test responses with
    known peaks. Same shape PEQ uses internally for residual
    estimation, so the designer's answer should be very close to the
    truth here."""
    return peq._bell_response_db(freqs, fc, q, gain_db)


def test_flat_response_yields_no_peqs():
    freqs = _log_freqs()
    measured = np.zeros_like(freqs)
    target_db = target.flat_target(freqs)
    peqs = peq.design_peq(measured, target_db, freqs)
    assert peqs == []


def test_single_peak_identified():
    """Synthetic response with a single +6 dB bell at 80 Hz, Q=4.
    The designer should pick a filter near 80 Hz, negative gain."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    measured = _bell(freqs, fc=80.0, q=4.0, gain_db=6.0)
    peqs = peq.design_peq(measured, target_db, freqs)
    assert len(peqs) >= 1
    p = peqs[0]
    # Picked center frequency should be close to 80 Hz (within an
    # eighth-octave on the log-spaced grid).
    assert abs(np.log2(p.freq / 80.0)) < 0.125
    # Cuts only ⇒ negative gain.
    assert p.gain < 0
    # Magnitude near the synthetic peak height (clamped to max_cut).
    assert -8 < p.gain < -3


def test_cuts_only_skips_dips():
    """A response with a -6 dB dip and no peaks should yield zero
    PEQs when cuts_only=True (default)."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    measured = -_bell(freqs, fc=120.0, q=3.0, gain_db=6.0)  # dip
    peqs = peq.design_peq(measured, target_db, freqs)
    assert peqs == []


def test_cuts_and_boosts_handles_dip():
    """With cuts_only=False, the designer is allowed to fill a dip
    with a boost. With max_boost=+3 dB and a 6 dB dip, the algorithm
    may stack two +3 dB filters at the same frequency to fully fill
    the dip — that's expected greedy behavior; refining 'don't
    redundantly stack' is Phase 2."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    measured = -_bell(freqs, fc=120.0, q=3.0, gain_db=6.0)  # dip
    peqs = peq.design_peq(
        measured, target_db, freqs,
        cuts_only=False, max_boost_db=3.0,
    )
    assert 1 <= len(peqs) <= 2
    # All filters should be boosts (non-negative gain).
    assert all(p.gain > 0 for p in peqs)
    # Picked frequencies cluster around the dip center.
    for p in peqs:
        assert abs(np.log2(p.freq / 120.0)) < 0.125
    # Per-filter cap respected.
    assert all(p.gain <= 3.0 + 1e-3 for p in peqs)


def test_max_filters_cap_respected():
    """Multiple peaks shouldn't blow past max_filters."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    measured = (
        _bell(freqs, fc=40.0, q=4, gain_db=6) +
        _bell(freqs, fc=80.0, q=4, gain_db=5) +
        _bell(freqs, fc=120.0, q=4, gain_db=4) +
        _bell(freqs, fc=180.0, q=4, gain_db=4) +
        _bell(freqs, fc=250.0, q=4, gain_db=3) +
        _bell(freqs, fc=320.0, q=4, gain_db=3)
    )
    peqs = peq.design_peq(
        measured, target_db, freqs,
        max_filters=3,
    )
    assert len(peqs) <= 3


def test_band_limited_to_modal_range():
    """A peak above the f_high cutoff should be ignored. We don't
    correct above ~Schroeder by default — that's the whole 20-350 Hz
    rule from Toole."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    # +6 dB peak at 1500 Hz — above the default f_high=350.
    measured = _bell(freqs, fc=1500.0, q=4.0, gain_db=6.0)
    peqs = peq.design_peq(measured, target_db, freqs)
    # No PEQs should be placed for this peak.
    assert peqs == []


def test_max_cut_db_clamps():
    """A 30 dB peak should be clamped to the max_cut limit (-10 dB
    by default). Bigger cuts than that are a sign the room needs
    acoustic treatment, not EQ."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    measured = _bell(freqs, fc=100.0, q=4.0, gain_db=30.0)
    peqs = peq.design_peq(measured, target_db, freqs)
    assert len(peqs) >= 1
    p = peqs[0]
    assert p.gain >= -10.0


def test_q_clamped_to_range():
    """Q outside [q_min, q_max] should be clamped. A very narrow
    peak (Q=20) should land at q_max=8.0."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    measured = _bell(freqs, fc=100.0, q=20.0, gain_db=6.0)
    peqs = peq.design_peq(
        measured, target_db, freqs, q_min=1.0, q_max=8.0,
    )
    assert len(peqs) >= 1
    assert all(1.0 <= p.q <= 8.0 for p in peqs)


def test_predicted_response_zero_for_empty_peqs():
    freqs = _log_freqs()
    pred = peq.predicted_response([], freqs)
    assert pred.shape == freqs.shape
    assert (pred == 0).all()


def test_predicted_response_negates_measured_peak_approximately():
    """If we synthesize a response with a +6 dB peak and run the
    designer, applying its predicted response back should mostly
    cancel the peak (residual within ~2 dB at the peak)."""
    freqs = _log_freqs()
    target_db = target.flat_target(freqs)
    measured = _bell(freqs, fc=80.0, q=4.0, gain_db=6.0)
    peqs = peq.design_peq(measured, target_db, freqs)
    pred_shift = peq.predicted_response(peqs, freqs)
    corrected = measured + pred_shift
    # Corrected response should be flatter — peak < 2 dB residual.
    band_mask = (freqs >= 40.0) & (freqs <= 200.0)
    assert float(np.max(np.abs(corrected[band_mask]))) < 2.0


def test_total_max_boost_zero_when_cuts_only():
    freqs = _log_freqs()
    measured = _bell(freqs, fc=80, q=4, gain_db=6) - _bell(freqs, fc=200, q=4, gain_db=4)
    peqs = peq.design_peq(measured, target.flat_target(freqs), freqs)
    assert peq.total_max_boost_db(peqs) == 0.0


def test_design_peq_validates_lengths():
    import pytest
    freqs = np.linspace(20, 20000, 100)
    measured = np.zeros(100)
    bad_target = np.zeros(50)
    with pytest.raises(ValueError, match="length mismatch"):
        peq.design_peq(measured, bad_target, freqs)


# ---------- target curve sanity --------------------------------------------


def test_flat_target_is_zero():
    freqs = _log_freqs()
    assert (target.flat_target(freqs) == 0).all()


def test_harman_target_subbass_shelf():
    """+4 dB at 40 Hz, 0 dB by 100 Hz, descending above."""
    freqs = np.array([20.0, 40.0, 60.0, 100.0, 1000.0, 10000.0])
    db = target.harman_target(freqs)
    assert db[0] == 4.0  # 20 Hz
    assert db[1] == 4.0  # 40 Hz
    assert db[2] == 4.0  # 60 Hz (boundary)
    assert abs(db[3]) < 0.1  # 100 Hz
    # -1 dB/octave from 100 Hz: at 1000 Hz that's -log2(10) ≈ -3.32
    assert abs(db[4] + np.log2(10)) < 0.1
    # at 10 kHz: -log2(100) ≈ -6.64
    assert abs(db[5] + np.log2(100)) < 0.1


def test_house_curve_interpolates():
    freqs = np.array([20.0, 1000.0, 10000.0])
    flat = target.house_curve(freqs, warmth=0.0)
    full = target.house_curve(freqs, warmth=1.0)
    half = target.house_curve(freqs, warmth=0.5)
    assert (flat == 0).all()
    # half should be midway between flat and full at every frequency.
    assert np.allclose(half, 0.5 * full)
