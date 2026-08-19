"""The trim give-back is measured in the band the verdict grades.

**The invariant this file exists to pin, in one sentence.** A give-back that
adjusts a trim must be measured with the same estimator, in the same averaging
domain, and over the SAME BANDS as the trim it adjusts and the verdict that
grades that trim.

Two of those three legs were already enforced before this change.
``linearization_fit``'s give-back carries a LOCKSTEP REQUIREMENT that its
average stay the trim solver's power-domain mean, whose stated reason is that
otherwise "the anchored trim would systematically mis-level the branch" — and
it guarded ~0.3 dB of averaging-domain error. The BAND was left unmatched, and
that cost 3.67 dB on a real horn tweeter, an order of magnitude more than the
leg that was guarded.

**The incident these tests are built from** (jts3, 2026-08-19; full evidence
chain in ``captures/wired-night-2026-08-19/run-log.md`` §10.9). The anchor took
``LinearizationFit.correction_giveback_db``, a power mean over each driver's own
CORE band, while ``realized_level_match`` grades the crossover halves. On a
compression-horn tweeter those barely overlap — core 2077-7949 Hz against a
graded 1649-3297 Hz — so the horn's 3-8 kHz correction bought back level where
the verdict is not taken. The committed pair then carried
``giveback_tweeter - giveback_woofer`` as pure inter-driver error: the tweeter
shipped +3.67 dB hotter than its own raw measurement asked, the realized level
landed 3.01 dB apart against a 3.0 dB tolerance, and the round was refused by
0.01 dB. The owner, listening to the config that shipped through the same path,
called it "a little bright" without being told what to listen for.

The tests below pin the class BOTH ways, because a level-accounting fix that
only stops the bad case is half a fix:

* a correction lying OUTSIDE the graded band must not move the committed pair
  (the defect), and
* a correction lying INSIDE it must still be given back (the purpose).
"""

from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.intervention import anchor_trims
from jasper.audio_measurement.program_analysis import (
    realized_branch_level_match,
    solve_branch_trims,
)

WOOFER = "woofer"
TWEETER = "tweeter"

#: The jts3 geometry the incident was measured on, rounded to the values the
#: journal printed.
FC_HZ = 1648.7
WOOFER_SPAN_HZ = (824.4, 1648.7)
TWEETER_SPAN_HZ = (1648.7, 3297.4)

#: The tweeter's own core band on that speaker — where ``correction_giveback_db``
#: is averaged, and the band whose mismatch with the graded span is the defect.
TWEETER_CORE_BAND_HZ = (2077.2, 7949.3)
WOOFER_CORE_BAND_HZ = (150.0, 1291.4)

#: A compression-horn tweeter sitting this far above the woofer for the same
#: electrical drive. Declared on the box as 108.5 - 83.3 dB sensitivity minus a
#: 14.4 dB L-pad = +10.80 dB; measured by the trim solve as 10.85 dB.
TWEETER_EXCESS_DB = 10.8


def _grid() -> np.ndarray:
    """A linear-frequency bin grid, the shape the FFT estimator reads."""
    return np.linspace(100.0, 9000.0, 8901)


def _flat(freqs: np.ndarray, level_db: float) -> np.ndarray:
    return np.full(freqs.size, 10.0 ** (level_db / 20.0), dtype=complex)


def _shelf(
    freqs: np.ndarray, *, corner_hz: float, depth_db: float,
) -> np.ndarray:
    """A cut of ``depth_db`` applied only ABOVE ``corner_hz``.

    Deliberately a hard step rather than a realistic RBJ shelf: the question
    under test is WHICH BAND a correction's energy lands in, and a step makes
    "inside the graded span" and "outside it" exact rather than approximate, so
    a failure is unambiguous about the band and not about filter skirts.
    """
    gain = np.ones(freqs.size)
    gain[freqs > corner_hz] = 10.0 ** (-abs(depth_db) / 20.0)
    return gain.astype(complex)


def _power_mean_db(values_db: np.ndarray) -> float:
    return float(10.0 * np.log10(np.mean(10.0 ** (values_db / 10.0))))


def _core_band_giveback_db(
    freqs: np.ndarray,
    before: np.ndarray,
    after: np.ndarray,
    band_hz: tuple[float, float],
) -> float:
    """The OLD rule's give-back: the power-domain before-vs-after level delta
    over the driver's own CORE band.

    Emulated here rather than driven through ``fit_driver_linearization``
    because the fit engine would need a whole envelope/correction solve to
    produce one scalar, and the scalar is all that reaches the anchor. The
    emulation is the same quantity by the same rule — a power mean of the
    magnitude delta over the core mask — which is what
    ``LinearizationFit.correction_giveback_db``'s own SSOT comment defines.
    """
    mask = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    before_db = 20.0 * np.log10(np.abs(before[mask]))
    after_db = 20.0 * np.log10(np.abs(after[mask]))
    return _power_mean_db(before_db) - _power_mean_db(after_db)


def _level_band_giveback_db(
    freqs: np.ndarray,
    w_before: np.ndarray,
    t_before: np.ndarray,
    w_after: np.ndarray,
    t_after: np.ndarray,
) -> dict[str, float]:
    """The NEW rule's give-back, through the production estimator.

    Same function, same bands, same averaging as the trim solve and the
    verdict — which is the whole invariant. Because both reads go through the
    identical call, the estimator's known +0.54 dB linear-grid systematic
    appears in each and cancels in the difference.
    """
    _rw, _rt, level_w_before, level_t_before = solve_branch_trims(
        freqs, w_before, t_before, FC_HZ,
        woofer_span_hz=WOOFER_SPAN_HZ, tweeter_span_hz=TWEETER_SPAN_HZ,
    )
    _rw2, _rt2, level_w_after, level_t_after = solve_branch_trims(
        freqs, w_after, t_after, FC_HZ,
        woofer_span_hz=WOOFER_SPAN_HZ, tweeter_span_hz=TWEETER_SPAN_HZ,
    )
    return {
        WOOFER: float(level_w_before - level_w_after),
        TWEETER: float(level_t_before - level_t_after),
    }


def _raw_trim_db(
    freqs: np.ndarray, W: np.ndarray, T: np.ndarray,
) -> dict[str, float]:
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs, W, T, FC_HZ,
        woofer_span_hz=WOOFER_SPAN_HZ, tweeter_span_hz=TWEETER_SPAN_HZ,
    )
    return {WOOFER: float(trim_w), TWEETER: float(trim_t)}


def _realized_error_db(
    freqs: np.ndarray,
    w_lin: np.ndarray,
    t_lin: np.ndarray,
    committed: dict[str, float],
) -> float:
    """The verdict: how far apart the two branches actually land."""
    return float(
        realized_branch_level_match(
            freqs, w_lin, t_lin, FC_HZ,
            trim_w_db=committed[WOOFER],
            trim_t_db=committed[TWEETER],
            woofer_span_hz=WOOFER_SPAN_HZ,
            tweeter_span_hz=TWEETER_SPAN_HZ,
        ).difference_db
    )


def _horn_case() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """A hot horn tweeter whose correction lives ABOVE the graded span.

    Returns ``(freqs, W, T, w_lin, t_lin)``. The woofer takes no correction, so
    every number below is attributable to the tweeter's band placement alone.
    """
    freqs = _grid()
    W = _flat(freqs, 0.0)
    T = _flat(freqs, TWEETER_EXCESS_DB)
    # 8 dB of cut starting above the graded span's top edge — the shape of a
    # CD-horn's HF correction, which is where a horn's linearization spends.
    correction = _shelf(freqs, corner_hz=TWEETER_SPAN_HZ[1], depth_db=8.0)
    return freqs, W, T, W, T * correction


# --------------------------------------------------------------------------- #
# the invariant
# --------------------------------------------------------------------------- #


def test_the_committed_pair_lands_level_when_the_giveback_shares_the_verdicts_band():
    """The invariant, stated as the outcome it exists to produce.

    A trim solved by this estimator, adjusted by a give-back measured through
    the SAME estimator over the SAME bands, must land the two branches level
    when that same estimator grades them. Not approximately — the terms cancel
    algebraically:

        realized = (level_t_pre - level_w_pre) + (raw_t - raw_w)

    and ``raw`` is by construction the pair that zeroes exactly that. The
    normalize shift subtracts from both roles identically, so it cannot appear.
    """
    freqs, W, T, w_lin, t_lin = _horn_case()
    raw = _raw_trim_db(freqs, W, T)
    giveback = _level_band_giveback_db(freqs, W, T, w_lin, t_lin)
    committed, _shift = anchor_trims(
        roles=(WOOFER, TWEETER), anchor_base_db=raw, giveback_db=giveback,
    )

    assert _realized_error_db(freqs, w_lin, t_lin, committed) == pytest.approx(
        0.0, abs=1e-9
    )


def test_a_correction_outside_the_graded_band_cannot_move_the_committed_pair():
    """THE DEFECT, pinned so it cannot come back through this route.

    The tweeter's correction is entirely above the graded span. It therefore
    changes nothing the verdict measures, so it must buy back nothing — the
    committed pair must be the raw measured pair.

    The old core-band rule fails exactly here, and the assertion below on it is
    the regression's teeth: the core band SEES that 8 dB cut, hands it to the
    anchor, and the anchor spends it relaxing a trim graded somewhere else.
    """
    freqs, W, T, w_lin, t_lin = _horn_case()
    raw = _raw_trim_db(freqs, W, T)

    band_matched = _level_band_giveback_db(freqs, W, T, w_lin, t_lin)
    assert band_matched[TWEETER] == pytest.approx(0.0, abs=1e-9), (
        "a cut outside the graded span changed nothing the verdict reads"
    )

    core_band = {
        WOOFER: _core_band_giveback_db(freqs, W, w_lin, WOOFER_CORE_BAND_HZ),
        TWEETER: _core_band_giveback_db(freqs, T, t_lin, TWEETER_CORE_BAND_HZ),
    }
    assert core_band[TWEETER] > 3.0, (
        "fixture check: the core band must SEE the out-of-band cut, otherwise "
        "this test is not exercising the defect at all"
    )

    committed_new, _s1 = anchor_trims(
        roles=(WOOFER, TWEETER), anchor_base_db=raw, giveback_db=band_matched,
    )
    committed_old, _s2 = anchor_trims(
        roles=(WOOFER, TWEETER), anchor_base_db=raw, giveback_db=core_band,
    )

    # The fix: the committed pair is the measured pair, and it grades level.
    assert committed_new[TWEETER] == pytest.approx(raw[TWEETER], abs=1e-9)
    assert _realized_error_db(freqs, w_lin, t_lin, committed_new) == pytest.approx(
        0.0, abs=1e-9
    )

    # The defect: the old rule ships the tweeter hot by the give-back
    # differential, and the verdict sees every dB of it.
    hot_by_db = committed_old[TWEETER] - committed_new[TWEETER]
    assert hot_by_db > 3.0
    assert _realized_error_db(
        freqs, w_lin, t_lin, committed_old
    ) == pytest.approx(hot_by_db, abs=1e-6)


def test_a_correction_inside_the_graded_band_is_still_given_back():
    """THE PURPOSE, pinned so the fix cannot be "make the give-back zero".

    A give-back exists because a branch's own cuts lower it, and the trim solve
    placed that branch before the cuts existed. When the cut IS inside the band
    the verdict reads, every dB of it must come back — otherwise the fix trades
    a hot tweeter for a dull one, which is the 2026-07-24 failure in the other
    direction ("leaving the whole tweeter band ~3 dB low").
    """
    freqs = _grid()
    W = _flat(freqs, 0.0)
    T = _flat(freqs, TWEETER_EXCESS_DB)
    # A 4 dB cut across the whole tweeter, so it is unambiguously inside the
    # graded span as well as everywhere else.
    t_lin = T * _shelf(freqs, corner_hz=0.0, depth_db=4.0)

    raw = _raw_trim_db(freqs, W, T)
    giveback = _level_band_giveback_db(freqs, W, T, W, t_lin)

    assert giveback[TWEETER] == pytest.approx(4.0, abs=1e-9), (
        "an in-band cut must be given back in full, not swallowed"
    )

    committed, _shift = anchor_trims(
        roles=(WOOFER, TWEETER), anchor_base_db=raw, giveback_db=giveback,
    )
    assert committed[TWEETER] == pytest.approx(raw[TWEETER] + 4.0, abs=1e-9)
    assert _realized_error_db(freqs, W, t_lin, committed) == pytest.approx(
        0.0, abs=1e-9
    )


def test_the_giveback_carries_no_cross_branch_coupling():
    """The woofer's own correction must not move the tweeter's committed trim.

    ``anchor_trims`` normalizes with a shared shift, which preserves the pair's
    SPACING exactly. This pins that the give-back leaves that property intact:
    correcting one branch changes the pair's spacing by that branch's own
    in-band give-back and by nothing belonging to the other.
    """
    freqs = _grid()
    W = _flat(freqs, 0.0)
    T = _flat(freqs, TWEETER_EXCESS_DB)
    w_lin = W * _shelf(freqs, corner_hz=0.0, depth_db=2.0)

    raw = _raw_trim_db(freqs, W, T)
    giveback = _level_band_giveback_db(freqs, W, T, w_lin, T)
    committed, _shift = anchor_trims(
        roles=(WOOFER, TWEETER), anchor_base_db=raw, giveback_db=giveback,
    )

    assert giveback[WOOFER] == pytest.approx(2.0, abs=1e-9)
    assert giveback[TWEETER] == pytest.approx(0.0, abs=1e-9)
    spacing = committed[TWEETER] - committed[WOOFER]
    assert spacing == pytest.approx(raw[TWEETER] - raw[WOOFER] - 2.0, abs=1e-9)
    assert _realized_error_db(freqs, w_lin, T, committed) == pytest.approx(
        0.0, abs=1e-9
    )


# --------------------------------------------------------------------------- #
# the banked jts3 runs — the wander was the differential, and nothing else
# --------------------------------------------------------------------------- #

#: Three banked stage-1 runs on jts3, 2026-08-19, read from the journal's own
#: ``correction.crossover_v2_linearization_giveback`` lines. ``anchored`` is
#: what the OLD cross-band rule committed.
JTS3_RUNS = (
    # (label,          raw_t,    gb_core_w, gb_core_t, anchored_t)
    ("attempt-01",     -10.841,  2.749,     6.740,     -6.849),
    ("attempt-02",     -10.757,  2.749,     6.679,     -6.826),
    ("round-19",       -10.850,  2.985,     6.656,     -7.180),
)


@pytest.mark.parametrize("label,raw_t,gb_w,gb_t,anchored_t", JTS3_RUNS)
def test_the_banked_runs_anchor_is_exactly_raw_plus_the_giveback_differential(
    label, raw_t, gb_w, gb_t, anchored_t,
):
    """The decomposition that identified the defect, pinned on real data.

    With the woofer's raw trim at 0.0 the shared normalize shift is the
    woofer's own give-back, so the tweeter's committed trim reduces to

        anchored_t == raw_t + (gb_t - gb_w)

    and the second term is pure inter-driver error: it is not a level the
    tweeter gave up in the band anyone grades. Reproducing all three banked
    rounds to a thousandth of a dB is what established that the anchor's
    run-to-run wander was this differential rather than measurement noise.
    """
    committed, shift = anchor_trims(
        roles=(WOOFER, TWEETER),
        anchor_base_db={WOOFER: 0.0, TWEETER: raw_t},
        giveback_db={WOOFER: gb_w, TWEETER: gb_t},
    )
    # The identity itself is exact — no tolerance is being spent on it.
    assert shift == pytest.approx(gb_w, abs=1e-9)
    assert committed[TWEETER] == pytest.approx(raw_t + (gb_t - gb_w), abs=1e-9)

    # Against the JOURNAL's own committed value the budget is rounding, and it
    # is stated rather than tuned: the journal rounds every term to 3 dp
    # independently, so `raw_t`, `gb_t` and `gb_w` each carry +/-0.0005 and
    # their combination carries up to +/-0.0015 against a separately-rounded
    # `anchored_t`. attempt-02 lands exactly there (-6.827 recomputed against a
    # printed -6.826), which is the rounding showing, not the identity failing.
    assert committed[TWEETER] == pytest.approx(anchored_t, abs=2e-3)


def test_the_anchor_wandered_further_than_the_measurement_it_was_built_from():
    """Why the fix is a stability fix as well as a level fix.

    Across the three banked runs the RAW measurement holds to 0.093 dB while
    the committed anchor wanders 0.354 dB — the anchor is less repeatable than
    its own input, which can only happen if it is carrying a term the input
    does not have. That term is the give-back differential, and it is what
    flipped round 19: the woofer's core-band give-back grew 0.236 dB and the
    round crossed a 3.0 dB bar it had cleared twice before.
    """
    raw_spread = max(r[1] for r in JTS3_RUNS) - min(r[1] for r in JTS3_RUNS)
    anchored_spread = max(r[4] for r in JTS3_RUNS) - min(r[4] for r in JTS3_RUNS)

    assert raw_spread == pytest.approx(0.093, abs=5e-4)
    assert anchored_spread == pytest.approx(0.354, abs=5e-4)
    assert anchored_spread > raw_spread * 3.0, (
        "the committed anchor must not be several times less repeatable than "
        "the measurement it is built from"
    )
