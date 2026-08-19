# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The sub-sample timeline-slip detector: sensitivity, floor, and its limits.

The gate constant is MEASURED here, not asserted. Two things make that
measurement mean something rather than being a self-consistent loop:

* :func:`test_simulated_noise_reproduces_the_hardware_statistic` calibrates
  the battery's synthetic locate noise against the number D7 measured on 28
  real captures, so "sd = 0.10" is anchored to hardware rather than chosen.
* the false-rejection and detection batteries are then read at THAT operating
  point, with the higher-noise rows kept as declared stress margin.

The rest checks that the detector finds the slips the Stage-0 bank proved are
real, does not find slips in the two things that legitimately look like one
(a real driver-to-driver delay, and honest clock drift), and does not claim
the one-sample sensitivity this structure cannot deliver.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement.timeline_slip import (
    SLIP_GATE_SAMPLES,
    STEP_RSS_RATIO,
    TimelineStepFit,
    fit_timeline_step,
    slip_rejects_capture,
)

# The interleaved MEASURE layout this detector exists for: w1 t1 w2 t2 w3 t3
# (design 5.4 / #1668's N=3), at the composer's own geometry -- 2 s guard,
# 4 s woofer, 3 s tweeter, ~1 s MESM gaps -- rounded to whole samples at
# 48 kHz. Exact spacing is not load-bearing; the interleave is.
_ROLES = ("woofer", "tweeter", "woofer", "tweeter", "woofer", "tweeter")
_STARTS = (96_000, 336_000, 528_000, 768_000, 960_000, 1_200_000)

# A REAL physical offset between the drivers, which the role constants must
# absorb rather than report as a slip. ~0.6 ms is an ordinary two-way value.
_ACOUSTIC = {"woofer": 40.0, "tweeter": 11.0}

# Ordinary mic-vs-playback clock offset; the Stage-0 bank measured +32 ppm on
# this rig class, so drift is modelled at that scale rather than at zero.
_EPSILON = 32e-6

#: The hardware operating point, established by the calibration test below.
_HARDWARE_SD = 0.10

#: D7's measured range for the shipped spread statistic on 28 real captures.
_D7_SPREAD_MIN, _D7_SPREAD_MAX = 0.038, 0.299


def _capture(
    *,
    step: float = 0.0,
    cut: int = 0,
    noise_sd: float = 0.0,
    seed: int = 0,
    epsilon: float = _EPSILON,
) -> tuple[tuple[int, ...], tuple[float, ...], tuple[str, ...]]:
    """Synthesize one capture's located positions.

    ``step`` samples are inserted before schedule index ``cut`` (so every
    index ``>= cut`` moves), ``noise_sd`` is per-location locate scatter.
    """
    rng = np.random.default_rng(seed)
    located = []
    for index, (start, role) in enumerate(zip(_STARTS, _ROLES)):
        value = _ACOUSTIC[role] + start * (1.0 + epsilon)
        if cut and index >= cut:
            value += step
        if noise_sd:
            value += float(rng.normal(0.0, noise_sd))
        located.append(value)
    return _STARTS, tuple(located), _ROLES


def _fit(**kwargs) -> TimelineStepFit:
    starts, located, roles = _capture(**kwargs)
    return fit_timeline_step(starts, located, roles)


def _spread_statistic(located: tuple[float, ...]) -> float:
    """The SHIPPED guard's statistic: max demeaned within-role deviation.

    Reimplemented here rather than imported because the point is to compare
    this battery's synthetic noise against the number D7 measured with it;
    reaching into ``program_analysis`` for it would drag a capture array and
    an ExcitationProgram in for no gain.
    """
    worst = 0.0
    for role in ("woofer", "tweeter"):
        idx = [i for i, r in enumerate(_ROLES) if r == role]
        scheduled = np.array([_STARTS[i] - _STARTS[idx[0]] for i in idx], float)
        measured = np.array([located[i] - located[idx[0]] for i in idx], float)
        resid = measured - scheduled * (1.0 + _EPSILON)
        worst = max(worst, float(np.abs(resid - resid.mean()).max()))
    return worst


# --------------------------------------------------------------------------- #
# calibration: what does the battery's "sd" mean in hardware terms?
# --------------------------------------------------------------------------- #


def test_simulated_noise_reproduces_the_hardware_statistic():
    """Anchor the battery to hardware before reading anything off it.

    D7 replayed 28 real captures through the shipped spread statistic and got
    0.038-0.299 samples, mean 0.116. Per-point locate noise of
    ``_HARDWARE_SD`` reproduces that distribution, which is what licenses
    calling sd = 0.10 the operating point and the higher rows stress margin.
    Without this the gate would be calibrated against an invented noise level.
    """
    values = []
    for seed in range(4000):
        _, located, _ = _capture(noise_sd=_HARDWARE_SD, seed=seed)
        values.append(_spread_statistic(located))
    values = np.array(values)
    assert _D7_SPREAD_MIN < values.mean() < 0.20, (
        f"simulated spread mean {values.mean():.3f} no longer sits inside "
        f"D7's measured hardware range {_D7_SPREAD_MIN}-{_D7_SPREAD_MAX}"
    )
    # The observed hardware MAX should sit inside this distribution's body,
    # not out on a tail the simulation never reaches.
    assert np.percentile(values, 99) > _D7_SPREAD_MAX * 0.7


# --------------------------------------------------------------------------- #
# positive controls: the things that must never be called a slip
# --------------------------------------------------------------------------- #


def test_clean_capture_is_not_rejected():
    """An honest capture, with real drift and a real driver offset, resolves
    no slip."""
    fit = _fit(noise_sd=_HARDWARE_SD, seed=11)
    assert fit.resolvable
    assert not slip_rejects_capture(fit)


def test_real_driver_delay_is_absorbed_not_reported():
    """A genuine woofer-vs-tweeter offset is physics, never a slip.

    The failure mode the per-role constants exist to prevent, so it gets its
    own test: 2 ms of real path difference is far bigger than any slip this
    gate rejects, and must still read clean.
    """
    starts, located, roles = _capture(noise_sd=0.0)
    inflated = tuple(
        value + (96.0 if role == "woofer" else 0.0)
        for value, role in zip(located, roles)
    )
    assert not slip_rejects_capture(fit_timeline_step(starts, inflated, roles))


def test_pure_clock_drift_is_not_a_slip():
    """Drift is a slope, a slip is a step; a steep but honest slope must not
    trip the gate."""
    assert not slip_rejects_capture(
        _fit(noise_sd=0.05, epsilon=400e-6, seed=5)
    )


@pytest.mark.parametrize(
    "noise_sd, max_false_rejection_pct",
    [
        (0.05, 0.0),
        (_HARDWARE_SD, 0.0),   # the hardware operating point: none, ever
        (0.20, 0.0),           # 2x worse than anything observed
        (0.30, 2.0),           # 3x worse; the gate is allowed to fray here
    ],
)
def test_false_rejection_rate_on_clean_captures(noise_sd, max_false_rejection_pct):
    """THE floor measurement, and the D7 lesson encoded as a bound.

    A false rejection costs a retake and, in the D7 incident, a round; so the
    requirement at and below the hardware operating point is ZERO, and only
    the declared stress rows are allowed any.
    """
    rejected = 0
    worst = 0.0
    for seed in range(1000):
        fit = _fit(noise_sd=noise_sd, seed=seed)
        worst = max(worst, abs(fit.step_samples))
        if slip_rejects_capture(fit):
            rejected += 1
    pct = 100.0 * rejected / 1000
    assert pct <= max_false_rejection_pct, (
        f"{pct:.2f}% of clean captures rejected at locate-noise sd={noise_sd} "
        f"(bound {max_false_rejection_pct}%); worst spurious step "
        f"{worst:.3f} vs gate {SLIP_GATE_SAMPLES}"
    )


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("step", [2.0, -2.0, 7.0, -7.0])
@pytest.mark.parametrize("cut", [1, 2, 3, 4, 5])
def test_detects_slips_at_every_interior_cut(step, cut):
    """A slip must be caught wherever in the program it lands — between two
    per-driver segments is exactly the case that biases the woofer-vs-tweeter
    comparison the M*C/P composition trusts."""
    fit = _fit(step=step, cut=cut, noise_sd=_HARDWARE_SD, seed=3)
    assert slip_rejects_capture(fit), (
        f"missed a {step:+g}-sample slip at cut {cut}: "
        f"step={fit.step_samples:.3f} rss_ratio={fit.rss_ratio:.3f}"
    )
    # MAGNITUDE only — the sign is not recoverable at even cuts; see
    # test_slip_location_and_sign_are_ambiguous_at_even_cuts for the measured
    # attribution table and why the gate is unaffected by it.
    assert abs(fit.step_samples) == pytest.approx(abs(step), abs=0.5)


def test_slip_location_and_sign_are_ambiguous_at_even_cuts():
    """The attribution limit, measured and pinned rather than discovered later.

    On the interleaved layout a slip at an EVEN schedule index shifts whole
    woofer/tweeter pairs, and the per-role constants then admit an equally
    good mirror solution of the opposite sign at another cut. Detection and
    magnitude are unharmed — the gate reads magnitude alone — but a forensic
    reader must not take the signed value or ``cut_index`` as fact.

    This is a claim about the MODEL, so it is asserted in both directions:
    odd cuts attribute exactly, even cuts do not.
    """
    def attribution(cut: int) -> float:
        agree = total = 0
        for seed in range(200):
            fit = _fit(step=3.0, cut=cut, noise_sd=_HARDWARE_SD, seed=seed)
            if slip_rejects_capture(fit):
                total += 1
                agree += fit.cut_index == cut
        return agree / max(total, 1)

    for odd_cut in (1, 3, 5):
        assert attribution(odd_cut) > 0.95, (
            f"cut {odd_cut} used to attribute exactly and no longer does"
        )
    for even_cut in (2, 4):
        assert attribution(even_cut) < 0.90, (
            f"cut {even_cut} now attributes better than the mirror-solution "
            "degeneracy allows — if a model change earned that, the "
            "TimelineStepFit docstring's 'read the magnitude' rule is stale"
        )


@pytest.mark.parametrize(
    "step, label",
    [
        (1.986, "Stage-0 p001A — the slip today's spread guard passes"),
        (7.008, "Stage-0 p004B — one whole USB isochronous packet"),
    ],
)
def test_detects_the_two_real_stage0_slips(step, label):
    """The two silent USB slips the Stage-0 bank actually recorded.

    Their exact geometry is synthesized rather than shipped as WAV fixtures:
    the bank's own size-bounded subset is 71 MB and the full set ~870 MB,
    which is not a size a test suite should carry when the quantity under
    test is a sample count. ``+1.986`` is the one that matters most — 41 us,
    twice the 20 us relative-phase bar, and inside today's guard's blind spot.

    Measured across 1000 seeds rather than one, because a single seed would
    hide a marginal detection rate.
    """
    detected = sum(
        slip_rejects_capture(
            _fit(step=step, cut=2, noise_sd=_HARDWARE_SD, seed=seed)
        )
        for seed in range(1000)
    )
    assert detected >= 950, (
        f"{label}: detected {detected}/1000 at the hardware operating point"
    )


def test_one_sample_slip_is_honestly_out_of_reach():
    """The declared LIMIT, pinned so it cannot be quietly overclaimed.

    A one-sample (20.833 us) slip is NOT reliably caught by this structure,
    and the constant's docstring says so. Catching it would need a gate near
    0.6, which false-rejects 14 % of clean captures at sd = 0.30 — the D7
    failure mode rebuilt deliberately. This test exists so a future edit that
    claims one-sample sensitivity has to delete an assertion that says
    otherwise.
    """
    detected = sum(
        slip_rejects_capture(
            _fit(step=1.0, cut=2, noise_sd=_HARDWARE_SD, seed=seed)
        )
        for seed in range(500)
    )
    assert detected < 100, (
        f"one-sample slips are now detected {detected}/500 — if this is real, "
        "SLIP_GATE_SAMPLES' stated limit and its false-rejection evidence both "
        "need re-deriving, not just this bound relaxing"
    )


# --------------------------------------------------------------------------- #
# controls on the harness itself
# --------------------------------------------------------------------------- #


def test_detection_control_pair_shares_everything_but_the_step():
    """The no-op control: same geometry, same seed, same noise — the ONLY
    difference is the step. If this pair ever agrees, the batteries above are
    measuring their own scaffolding rather than the detector."""
    common = dict(cut=2, noise_sd=_HARDWARE_SD, seed=42)
    assert not slip_rejects_capture(_fit(step=0.0, **common))
    assert slip_rejects_capture(_fit(step=3.0, **common))


def test_unresolvable_fit_never_rejects():
    """Too few sweeps is missing evidence, never a fault."""
    fit = fit_timeline_step(
        (96_000.0, 336_000.0, 528_000.0),
        (40.0, 11.0, 500.0),
        ("woofer", "tweeter", "woofer"),
    )
    assert not fit.resolvable
    assert not slip_rejects_capture(fit)
    assert fit.step_samples == 0.0


def test_parallel_sequence_lengths_are_enforced():
    with pytest.raises(ValueError):
        fit_timeline_step((1.0, 2.0), (1.0,), ("woofer", "tweeter"))


def test_both_admission_rules_are_required():
    """Size alone must not reject: a step-sized wobble that explains nothing
    is the false positive the RSS ratio exists to stop."""
    assert not slip_rejects_capture(
        TimelineStepFit(
            step_samples=5.0, cut_index=2, rss_ratio=0.9, resolvable=True
        )
    )
    assert slip_rejects_capture(
        TimelineStepFit(
            step_samples=5.0, cut_index=2, rss_ratio=STEP_RSS_RATIO,
            resolvable=True,
        )
    )
